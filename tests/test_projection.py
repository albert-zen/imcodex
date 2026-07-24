from __future__ import annotations

from pathlib import Path

from imcodex.appserver import normalize_appserver_message
from imcodex.bridge import MessageProjector
from imcodex.bridge.outbound_artifacts import OutboundArtifactStager
from imcodex.bridge.message_pump import EMPTY_COMPLETED_TURN_TEXT
from imcodex.models import NativeThreadSnapshot
from imcodex.store import ConversationStore


def test_projector_renders_question_request_with_question_details() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "id": 99,
            "method": "item/tool/requestUserInput",
            "params": {
                "_request_id": "native-request-abcdef",
                "threadId": "thr_1",
                "turnId": "turn_1",
                "questions": [
                    {"id": "color", "question": "Favorite color?"},
                    {"id": "size", "question": "Choose size"},
                ],
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "item/tool/requestUserInput"
    assert message.metadata["request_kind"] == "question"
    assert "color" in message.text
    assert "Favorite color?" in message.text
    assert "/answer native-request-abcdef color=value" in message.text


def test_projector_renders_approval_request_with_command_details() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "id": 101,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "_request_id": "native-request-approval",
                "threadId": "thr_1",
                "turnId": "turn_1",
                "command": "git status",
                "cwd": r"D:\work\alpha",
                "reason": "Inspect repo state",
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "item/commandExecution/requestApproval"
    assert message.metadata["request_kind"] == "approval"
    assert "git status" in message.text
    assert r"D:\work\alpha" in message.text
    assert "Inspect repo state" in message.text


def test_projector_suppresses_late_tool_progress_after_final_answer() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    final_message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Here is the final answer.",
                },
            },
        },
        store,
    )
    late_tool = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "cmd_1",
                    "type": "commandExecution",
                    "command": "pytest -q",
                },
            },
        },
        store,
    )

    assert final_message is not None
    assert final_message.message_type == "agentMessage"
    assert final_message.metadata["phase"] == "final_answer"
    assert late_tool is None


def test_projector_reopens_commentary_when_native_work_starts_after_final_answer() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    final_message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "answer_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "First answer.",
                },
            },
        },
        store,
    )

    resumed = projector.resume_turn_output(
        thread_id="thr_1",
        turn_id="turn_1",
        store=store,
    )
    commentary = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "commentary_2",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Continuing after the queued steer.",
                },
            },
        },
        store,
    )

    assert final_message is not None
    assert resumed is True
    assert commentary is not None
    assert commentary.message_type == "agentMessage"
    assert commentary.metadata["phase"] == "commentary"
    assert commentary.text == "Continuing after the queued steer."


def test_projector_preserves_distinct_native_items_with_identical_text() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    messages = [
        projector.project_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr_1",
                    "turnId": "turn_1",
                    "item": {
                        "id": item_id,
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "Still working.",
                    },
                },
            },
            store,
        )
        for item_id in ("commentary_1", "commentary_2")
    ]

    assert [message.text for message in messages if message is not None] == [
        "Still working.",
        "Still working.",
    ]
    assert [
        message.metadata["native_item_id"]
        for message in messages
        if message is not None
    ] == ["commentary_1", "commentary_2"]


def test_projector_deduplicates_replayed_native_item_by_identity() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()
    notification = {
        "method": "item/completed",
        "params": {
            "threadId": "thr_1",
            "turnId": "turn_1",
            "item": {
                "id": "commentary_1",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "Still working.",
            },
        },
    }

    first = projector.project_notification(notification, store)
    replay = projector.project_notification(notification, store)

    assert first is not None
    assert replay is None


def test_projector_keeps_native_item_identity_after_resuming_output() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()
    notification = {
        "method": "item/completed",
        "params": {
            "threadId": "thr_1",
            "turnId": "turn_1",
            "item": {
                "id": "answer_1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "Done.",
            },
        },
    }

    assert projector.project_notification(notification, store) is not None
    assert projector.resume_turn_output(
        thread_id="thr_1",
        turn_id="turn_1",
        store=store,
    )

    assert projector.project_notification(notification, store) is None


def test_projector_accepts_native_item_when_active_turn_hint_differs() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_hint", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_native",
                "item": {
                    "id": "answer_native",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Native output wins.",
                },
            },
        },
        store,
    )

    assert message is not None
    assert message.text == "Native output wins."
    assert message.metadata["native_item_id"] == "answer_native"


def test_projector_labels_plan_updates_as_distinct_progress() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "explanation": "Checking delivery recovery.",
                "plan": [
                    {"step": "Reproduce failure", "status": "completed"},
                    {"step": "Verify restart", "status": "in_progress"},
                ],
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "turn/plan/updated"
    assert message.text == (
        "[Plan update]\n"
        "Checking delivery recovery.\n"
        "[completed] Reproduce failure\n"
        "[in_progress] Verify restart"
    )


def test_projector_preserves_native_generated_image_on_terminal_message(tmp_path) -> None:
    image_path = tmp_path / "generated.png"
    from PIL import Image

    Image.new("RGB", (2, 2), (1, 2, 3)).save(image_path)
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", str(tmp_path))
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector(
        artifact_stager=OutboundArtifactStager(tmp_path / "outbound-media")
    )

    image_message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "image_1",
                    "type": "imageGeneration",
                    "status": "completed",
                    "result": "generated",
                    "savedPath": str(image_path),
                },
            },
        },
        store,
    )
    final = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "answer_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "The generated image is attached.",
                },
            },
        },
        store,
    )

    assert image_message is None
    assert final is not None
    assert len(final.artifacts) == 1
    assert final.artifacts[0].kind == "image"
    assert Path(final.artifacts[0].local_path).is_relative_to(tmp_path / "outbound-media")


def test_projector_only_infers_images_from_final_answer_local_links(tmp_path) -> None:
    from PIL import Image

    notes_path = tmp_path / "testing.md"
    notes_path.write_text("# Tests\n", encoding="utf-8")
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (2, 2), (1, 2, 3)).save(image_path)
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", str(tmp_path))
    projector = MessageProjector(
        artifact_stager=OutboundArtifactStager(tmp_path / "outbound-media")
    )

    final = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "answer_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": (
                        f"[testing.md]({notes_path})\n"
                        f"[preview]({image_path})"
                    ),
                },
            },
        },
        store,
    )

    assert final is not None
    assert [artifact.filename for artifact in final.artifacts] == ["preview.png"]
    assert final.artifacts[0].kind == "image"


def test_recovered_final_answer_only_infers_images_from_local_links(tmp_path) -> None:
    from PIL import Image

    notes_path = tmp_path / "testing.md"
    notes_path.write_text("# Tests\n", encoding="utf-8")
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (2, 2), (1, 2, 3)).save(image_path)
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", str(tmp_path))
    projector = MessageProjector(
        artifact_stager=OutboundArtifactStager(tmp_path / "outbound-media")
    )

    messages = projector.project_recovered_turn(
        thread_id="thr_1",
        turn={
            "id": "turn_1",
            "status": "completed",
            "items": [
                {
                    "id": "answer_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": (
                        f"[testing.md]({notes_path})\n"
                        f"![preview]({image_path})"
                    ),
                }
            ],
        },
        store=store,
    )

    assert len(messages) == 1
    assert [artifact.filename for artifact in messages[0].artifacts] == [
        "preview.png"
    ]
    assert messages[0].artifacts[0].kind == "image"


def test_malformed_markdown_image_does_not_drop_final_answer(tmp_path) -> None:
    from PIL import Image

    image_path = tmp_path / "broken.png"
    Image.new("RGB", (2, 2), (1, 2, 3)).save(image_path)
    image_path.write_bytes(image_path.read_bytes()[:-8])
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", str(tmp_path))
    projector = MessageProjector(
        artifact_stager=OutboundArtifactStager(tmp_path / "outbound-media")
    )

    final = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "answer_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": f"Analysis is complete.\n\n[preview]({image_path})",
                },
            },
        },
        store,
    )

    assert final is not None
    assert final.text.startswith("Analysis is complete.")
    assert "Attachment delivery unavailable:" in final.text
    assert "output artifact is not a valid image" in final.text
    assert final.artifacts == []


def test_projector_releases_staged_artifact_when_turn_buffer_is_discarded(
    tmp_path,
) -> None:
    image_path = tmp_path / "generated.png"
    from PIL import Image

    Image.new("RGB", (2, 2), (1, 2, 3)).save(image_path)
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", str(tmp_path))
    stager = OutboundArtifactStager(tmp_path / "outbound-media")
    projector = MessageProjector(artifact_stager=stager)

    projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "image_1",
                    "type": "imageGeneration",
                    "savedPath": str(image_path),
                },
            },
        },
        store,
    )
    staged_path = next(iter(projector.message_pump.active_artifact_paths()))

    projector.discard_recovered_turn(thread_id="thr_1", turn_id="turn_1")
    stager.cleanup_unreferenced(set())

    assert not Path(staged_path).exists()


def test_projector_preserves_terminal_text_when_agent_message_has_no_phase() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    progress = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "text": "final text without phase",
                },
            },
        },
        store,
    )
    final = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert progress is not None
    assert final is not None
    assert final.text == "final text without phase"


def test_projector_does_not_emit_progress_for_agent_deltas() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "delta": "partial text",
            },
        },
        store,
    )

    assert message is None


def test_projector_uses_buffered_deltas_as_terminal_fallback() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    projector.project_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "delta": "hello ",
            },
        },
        store,
    )
    projector.project_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "delta": "world",
            },
        },
        store,
    )
    final = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert final is not None
    assert final.message_type == "turn/completed"
    assert final.metadata["status"] == "completed"
    assert final.text == "hello world"


def test_projector_emits_explicit_fallback_for_blank_completed_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    blank_item = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "   ",
                },
            },
        },
        store,
    )
    terminal = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert blank_item is None
    assert terminal is not None
    assert terminal.text == EMPTY_COMPLETED_TURN_TEXT


def test_protocol_mapping_prefers_native_request_id() -> None:
    event = normalize_appserver_message(
        {
            "method": "item/tool/requestUserInput",
            "params": {
                "_request_id": "transport-99",
                "requestId": "native-request-abcdef",
                "threadId": "thr_1",
                "turnId": "turn_1",
            },
        }
    )

    assert event.request_id == "native-request-abcdef"


def test_protocol_mapping_preserves_item_id_for_agent_delta() -> None:
    event = normalize_appserver_message(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "itemId": "item_123",
                "delta": "partial",
            },
        }
    )

    assert event.item_id == "item_123"
    assert event.category == "item"


def test_protocol_mapping_classifies_system_notifications_without_dropping_them() -> None:
    event = normalize_appserver_message(
        {
            "method": "model/rerouted",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "message": "Model upgraded automatically.",
            },
        }
    )

    assert event.kind == "model_rerouted"
    assert event.category == "system"


def test_protocol_mapping_classifies_goal_notifications() -> None:
    updated = normalize_appserver_message(
        {
            "method": "thread/goal/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "goal": {"status": "complete"},
            },
        }
    )
    cleared = normalize_appserver_message(
        {
            "method": "thread/goal/cleared",
            "params": {"threadId": "thr_1"},
        }
    )

    assert updated.kind == "thread_goal_updated"
    assert updated.category == "thread"
    assert cleared.kind == "thread_goal_cleared"


def test_projector_emits_turn_goal_updates() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "thread/goal/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "goal": {
                    "threadId": "thr_1",
                    "objective": "Finish the migration",
                    "status": "complete",
                },
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "thread/goal/updated"
    assert message.text == "Goal complete: Finish the migration"


def test_projector_suppresses_command_goal_updates_to_avoid_echoing_goal_commands() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "thread/goal/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": None,
                "goal": {
                    "threadId": "thr_1",
                    "objective": "Finish the migration",
                    "status": "active",
                },
            },
        },
        store,
    )

    assert message is None


def test_projector_preserves_changed_files_in_failed_turn_completion() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    progress = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "files_1",
                    "type": "fileChange",
                    "changes": [{"path": "src/imcodex/bridge/core.py"}],
                },
            },
        },
        store,
    )
    final = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "failed"},
            },
        },
        store,
    )

    assert progress is None
    assert final is not None
    assert "Turn failed." in final.text
    assert "Changed files:" in final.text
    assert "src/imcodex/bridge/core.py" in final.text


def test_projector_ignores_replayed_turn_started_for_older_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_2", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "inProgress"},
            },
        },
        store,
    )

    assert message is None
    assert store.get_active_turn("thr_1") == ("turn_2", "inProgress")


def test_projector_uses_native_turn_started_when_active_hint_is_empty() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    projector.project_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "inProgress"},
            },
        },
        store,
    )

    assert store.get_active_turn("thr_1") == ("turn_1", "inProgress")


def test_projector_accepts_native_request_even_when_active_turn_hint_differs() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_2", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "id": 99,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "requestId": "native-request-old",
                "threadId": "thr_1",
                "turnId": "turn_1",
                "command": "git status",
            },
        },
        store,
    )

    assert message is not None
    assert message.request_id == "native-request-old"
    assert store.match_pending_request("qq", "conv-1", "native-request-old") is not None


def test_projector_accepts_native_completion_when_active_turn_hint_differs() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_hint", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_native", "status": "failed"},
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "turn/completed"
    assert message.metadata["status"] == "failed"
    assert store.get_active_turn("thr_1") == ("turn_hint", "inProgress")


def test_projector_suppresses_late_output_for_stopped_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    store.suppress_turn("thr_1", "turn_1")
    store.clear_active_turn("thr_1")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "late final answer",
                },
            },
        },
        store,
    )
    completed = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "interrupted"},
            },
        },
        store,
    )

    assert message is None
    assert completed is None


def test_projector_emits_terminal_result_for_early_failed_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    final = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "failed"},
            },
        },
        store,
    )

    assert final is not None
    assert final.message_type == "turn/completed"
    assert final.metadata["status"] == "failed"
    assert final.text == "Turn failed."


def test_projector_reconciles_thread_status_even_when_system_messages_hidden() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "thr_1",
                "status": {"type": "idle"},
            },
        },
        store,
    )

    assert message is None
    assert store.get_active_turn("thr_1") is None


def test_projector_updates_thread_status_snapshot_before_visibility_filter() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_thread_snapshot(
        NativeThreadSnapshot(
            thread_id="thr_1",
            cwd=r"D:\work\alpha",
            preview="hello",
            status="inProgress",
        )
    )
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "thr_1",
                "status": {"type": "idle"},
            },
        },
        store,
    )

    assert message is None
    snapshot = store.get_thread_snapshot("thr_1")
    assert snapshot is not None
    assert snapshot.status == "idle"

from __future__ import annotations

from imcodex.appserver import normalize_appserver_message
from imcodex.bridge import MessageProjector
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
    assert message.message_type == "question_request"
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
    assert message.message_type == "approval_request"
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
    assert final_message.message_type == "turn_result"
    assert late_tool is None


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
    assert final.message_type == "turn_result"
    assert final.text == "hello world"


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


def test_projector_preserves_changed_files_in_failed_turn_result() -> None:
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


def test_projector_drops_request_from_stale_turn() -> None:
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

    assert message is None
    assert store.match_pending_request("qq", "conv-1", "native-request-old") is None


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
    assert final.message_type == "turn_result"
    assert final.text == "Turn failed."

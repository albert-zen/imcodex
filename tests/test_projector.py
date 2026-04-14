from __future__ import annotations

from pathlib import Path

from imcodex.models import PendingRequest
from imcodex.bridge import MessageProjector
from imcodex.bridge.request_registry import RequestRegistry
from imcodex.bridge.session_registry import SessionRegistry
from imcodex.bridge.turn_state import TurnStateMachine
from imcodex.bridge.visibility import VisibilityClassifier
from imcodex.store import ConversationStore


def test_command_execution_approval_is_projected_with_ticket() -> None:
    projector = MessageProjector()
    pending = PendingRequest(
        ticket_id="7",
        channel_id="demo",
        conversation_id="conv-1",
        kind="approval",
        summary="Run tests",
        payload={
            "command": "pytest -q",
            "cwd": "D:/repo/app",
            "reason": "Run project tests",
        },
        created_at=1.0,
    )

    message = projector.render_pending_request(pending)

    assert message.message_type == "approval_request"
    assert "[ticket 7]" in message.text
    assert "pytest -q" in message.text
    assert "/approve 7" in message.text


def test_tool_request_input_is_projected_with_answer_help() -> None:
    projector = MessageProjector()
    pending = PendingRequest(
        ticket_id="12",
        channel_id="demo",
        conversation_id="conv-1",
        kind="question",
        summary="Need more input",
        payload={
            "questions": [
                {
                    "id": "timezone",
                    "header": "Timezone",
                    "question": "Select a timezone",
                }
            ]
        },
        created_at=1.0,
    )

    message = projector.render_pending_request(pending)

    assert message.message_type == "question_request"
    assert "[ticket 12]" in message.text
    assert "timezone" in message.text
    assert "/answer 12 timezone=" in message.text


def test_projector_rebinds_reused_visibility_classifier_to_its_session_registry() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    stale_registry = SessionRegistry(store)
    fresh_registry = SessionRegistry(store)
    visibility = VisibilityClassifier(session_registry=stale_registry)

    projector = MessageProjector(session_registry=fresh_registry, visibility=visibility)

    assert projector.visibility.session_registry is fresh_registry


def test_approval_request_includes_available_context_fields() -> None:
    projector = MessageProjector()
    pending = PendingRequest(
        ticket_id="7",
        channel_id="demo",
        conversation_id="conv-1",
        kind="approval",
        summary="Run tests",
        payload={
            "command": "pytest -q",
            "cwd": "D:/repo/app",
            "reason": "Run project tests",
            "network": {"host": "api.example.com"},
        },
        created_at=1.0,
    )

    message = projector.render_pending_request(pending)

    assert "CWD: D:/repo/app" in message.text
    assert "Network: api.example.com" in message.text


def test_turn_completed_success_returns_model_text_only() -> None:
    projector = MessageProjector()

    message = projector.render_turn_completed(
        final_text="Implemented the webhook bridge.",
        command_summaries=["Executed `pytest -q`"],
        changed_files=["src/imcodex/api.py", "src/imcodex/backend.py"],
        failed=False,
        interrupted=False,
    )

    assert message.message_type == "turn_result"
    assert message.text == "Implemented the webhook bridge."


def test_turn_completed_failure_keeps_system_status_and_details() -> None:
    projector = MessageProjector()

    message = projector.render_turn_completed(
        final_text="Sandbox blocked command execution.",
        command_summaries=["Executed `pytest -q`"],
        changed_files=["src/imcodex/api.py"],
        failed=True,
        interrupted=False,
    )

    assert message.message_type == "turn_result"
    assert "Turn failed." in message.text
    assert "Sandbox blocked command execution." in message.text
    assert "Executed `pytest -q`" in message.text
    assert "src/imcodex/api.py" in message.text


def test_project_notification_attaches_turn_completion_to_conversation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    early_message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Hello from Codex",
                },
            },
        },
        store,
    )
    message = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert early_message is not None
    assert early_message.message_type == "turn_result"
    assert early_message.channel_id == "demo"
    assert early_message.conversation_id == "conv-1"
    assert "Hello from Codex" in early_message.text
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id is None
    assert binding.active_turn_status == "completed"
    assert message is None


def test_non_final_agent_message_item_is_projected_as_progress_update() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_progress",
                    "type": "agentMessage",
                    "phase": "draft",
                    "text": "I checked the repo structure and found the main bridge entrypoint.",
                },
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "turn_progress"
    assert message.channel_id == "demo"
    assert message.conversation_id == "conv-1"
    assert "main bridge entrypoint" in message.text


def test_non_final_agent_message_respects_commentary_visibility_toggle() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_progress",
                    "type": "agentMessage",
                    "phase": "draft",
                    "text": "I checked the repo structure and found the main bridge entrypoint.",
                },
            },
        },
        store,
    )

    assert message is None


def test_agent_message_delta_is_buffered_without_immediate_projection() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "delta": "Inspecting the active thread binding.",
            },
        },
        store,
    )

    assert message is None


def test_progress_and_final_answer_are_emitted_as_separate_messages() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    progress = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_progress",
                    "type": "agentMessage",
                    "phase": "draft",
                    "text": "I checked the repo structure and found the main bridge entrypoint.",
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
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "The bridge entrypoint is src/imcodex/application.py.",
                },
            },
        },
        store,
    )

    assert progress is not None
    assert progress.message_type == "turn_progress"
    assert final is not None
    assert final.message_type == "turn_result"
    assert "src/imcodex/application.py" in final.text


def test_late_tool_progress_after_final_answer_is_suppressed() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_toolcall_visibility("demo", "conv-1", enabled=True)
    projector = MessageProjector()

    final = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "The bridge entrypoint is src/imcodex/application.py.",
                },
            },
        },
        store,
    )
    progress = projector.project_notification(
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

    assert final is not None
    assert progress is None


def test_final_answer_followed_by_failed_completion_still_surfaces_failure() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    early = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "The bridge entrypoint is src/imcodex/application.py.",
                },
            },
        },
        store,
    )
    failed = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "failed"},
            },
        },
        store,
    )

    assert early is not None
    assert failed is not None
    assert failed.message_type == "turn_result"
    assert "Turn failed." in failed.text


def test_turn_started_updates_status_for_status_command() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
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
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id == "turn_1"
    assert binding.active_turn_status == "inProgress"


def test_server_request_resolved_clears_matching_pending_ticket() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="7",
        kind="approval",
        summary="Run tests",
        payload={"command": "pytest -q"},
        request_id="99",
        thread_id="thr_1",
        turn_id="turn_1",
    )
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "serverRequest/resolved",
            "params": {
                "threadId": "thr_1",
                "requestId": "99",
            },
        },
        store,
    )

    assert message is None
    assert store.get_pending_request("7") is None
    assert store.get_binding("demo", "conv-1").pending_request_ids == []


def test_server_request_resolved_is_ignored_for_unknown_request() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "serverRequest/resolved",
            "params": {
                "threadId": "thr_1",
                "requestId": "404",
            },
        },
        store,
    )

    assert message is None


def test_thread_name_updated_refreshes_local_label_without_emitting_message() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "thread/name/updated",
            "params": {
                "threadId": "thr_1",
                "name": "Investigate alpha",
            },
        },
        store,
    )

    assert message is None
    assert store.thread_label("thr_1") == "Investigate alpha"


def test_turn_diff_update_is_projected_as_progress_when_commentary_is_visible() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "summary": "Updated 2 files",
                "files": ["src/imcodex/bridge/core.py", "tests/test_service.py"],
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "turn_progress"
    assert "Updated 2 files" in message.text
    assert "src/imcodex/bridge/core.py" in message.text


def test_turn_diff_update_respects_commentary_visibility_toggle() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "summary": "Updated 2 files",
                "files": ["src/imcodex/bridge/core.py"],
            },
        },
        store,
    )

    assert message is None


def test_unknown_notification_is_ignored_without_crashing() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "future/unknown",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
            },
        },
        store,
    )

    assert message is None


def test_request_registry_backed_approval_request_still_renders() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "itemId": "cmd_1",
                "_request_id": "99",
                "command": "pytest -q",
                "cwd": r"D:\work\alpha",
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "approval_request"
    assert message.ticket_id == "1"
    assert "pytest -q" in message.text


def test_request_registry_backed_question_request_still_renders() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "itemId": "ask_1",
                "_request_id": "100",
                "questions": [
                    {
                        "id": "branch",
                        "question": "Which branch?",
                    }
                ],
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "question_request"
    assert message.ticket_id == "1"
    assert "branch" in message.text


def test_question_request_with_empty_questions_list_does_not_crash() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "_request_id": "native-1",
                "questions": [],
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "question_request"
    assert "/answer 1 question=value" in message.text


def test_stale_turn_terminal_messages_are_suppressed_after_newer_turn_starts() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    turn_state = TurnStateMachine()
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=turn_state,
    )

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
    projector.project_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_2", "status": "inProgress"},
            },
        },
        store,
    )

    final_item = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Old turn final answer",
                },
            },
        },
        store,
    )
    final_turn = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert final_item is None
    assert final_turn is None
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id == "turn_2"
    assert binding.active_turn_status == "inProgress"


def test_unknown_turn_terminal_messages_are_not_suppressed_after_restart() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        status="inProgress",
    )
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    early = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Recovered final answer",
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

    assert early is not None
    assert early.message_type == "turn_result"
    assert "Recovered final answer" in early.text
    assert final is None
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id is None
    assert binding.active_turn_status == "completed"


def test_persisted_active_turn_still_suppresses_stale_turn_after_restart() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_2",
        status="inProgress",
    )
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Old turn final answer",
                },
            },
        },
        store,
    )

    assert message is None
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id == "turn_2"
    assert binding.active_turn_status == "inProgress"


def test_completed_newer_turn_is_not_suppressed_after_restart_without_runtime_turn_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_1",
        status="inProgress",
    )
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_2",
        status="inProgress",
    )
    store.note_turn_completed(thread.thread_id, turn_id="turn_2", status="completed")
    binding = store.get_binding("demo", "conv-1")
    binding.active_turn_id = None
    binding.active_turn_status = "completed"
    store._save()

    reloaded = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    projector = MessageProjector(
        request_registry=RequestRegistry(reloaded),
        turn_state=TurnStateMachine(),
    )

    late_final = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Old turn final answer",
                },
            },
        },
        reloaded,
    )
    late_complete = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        reloaded,
    )

    assert late_final is not None
    assert late_final.message_type == "turn_result"
    assert late_final.text == "Old turn final answer"
    assert late_complete is None
    binding = reloaded.get_binding("demo", "conv-1")
    assert binding.active_turn_id is None
    assert binding.active_turn_status == "completed"


def test_unseen_newer_turn_is_not_dropped_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_1",
        status="inProgress",
    )
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_2",
        status="inProgress",
    )
    store.note_turn_completed(thread.thread_id, turn_id="turn_2", status="completed")
    binding = store.get_binding("demo", "conv-1")
    binding.active_turn_id = None
    binding.active_turn_status = "completed"
    store._save()

    reloaded = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    projector = MessageProjector(
        request_registry=RequestRegistry(reloaded),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_3",
                "item": {
                    "id": "item_final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Recovered newer turn output",
                },
            },
        },
        reloaded,
    )

    assert message is not None
    assert message.message_type == "turn_result"
    assert "Recovered newer turn output" in message.text


def test_stale_turn_requests_are_not_projected() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_1",
        status="inProgress",
    )
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_2",
        status="inProgress",
    )
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

    message = projector.project_notification(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "itemId": "item_old",
                "_request_id": "req-old",
                "command": "pytest -q",
            },
        },
        store,
    )

    assert message is None
    assert store.list_pending_requests("demo", "conv-1") == []


def test_delayed_turn_started_for_older_thread_does_not_replace_current_thread_binding() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    old_thread = store.record_thread("thr_old", cwd=r"D:\work\alpha", preview="old")
    new_thread = store.record_thread("thr_new", cwd=r"D:\work\alpha", preview="")
    store.set_active_thread("demo", "conv-1", old_thread.thread_id)
    store.set_active_thread("demo", "conv-1", new_thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thr_old",
                "turn": {"id": "turn_old", "status": "inProgress"},
            },
        },
        store,
    )

    assert message is None
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_thread_id == new_thread.thread_id
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None


def test_replayed_stale_turn_started_does_not_replace_current_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_1",
        status="inProgress",
    )
    store.set_active_turn(
        "demo",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_2",
        status="inProgress",
    )
    projector = MessageProjector(
        request_registry=RequestRegistry(store),
        turn_state=TurnStateMachine(),
    )

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
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_turn_id == "turn_2"
    assert binding.active_turn_status == "inProgress"


def test_turn_completion_still_routes_after_switching_active_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    first = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="first")
    second = store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="second")
    store.set_active_thread("demo", "conv-1", first.thread_id)
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
                    "text": "First thread reply",
                },
            },
        },
        store,
    )
    store.set_active_thread("demo", "conv-1", second.thread_id)

    final_message = projector.project_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_1",
                "turn": {"id": "turn_1", "status": "completed"},
            },
        },
        store,
    )

    assert message is not None
    assert message.channel_id == "demo"
    assert message.conversation_id == "conv-1"
    assert "First thread reply" in message.text
    assert final_message is None


def test_runtime_session_index_routes_thread_events_before_store_scan() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    registry = SessionRegistry(store)
    registry.bind_cwd("demo", "conv-1", thread.cwd)
    registry.bind_thread("demo", "conv-1", thread.thread_id)
    registry.bind_cwd("demo", "conv-2", thread.cwd)
    registry.bind_thread("demo", "conv-2", thread.thread_id)
    projector = MessageProjector(session_registry=registry)

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
                    "text": "Route using the runtime session index.",
                },
            },
        },
        store,
    )

    assert message is not None
    assert message.channel_id == "demo"
    assert message.conversation_id == "conv-2"


def test_projector_drops_late_thread_event_once_runtime_index_detaches_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    registry = SessionRegistry(store)
    registry.bind_cwd("demo", "conv-1", thread.cwd)
    registry.bind_thread("demo", "conv-1", thread.thread_id)
    store.set_selected_cwd("demo", "conv-1", r"D:\work\beta")
    registry.sync("demo", "conv-1")
    projector = MessageProjector(session_registry=registry)

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "item_1",
                    "type": "agentMessage",
                    "phase": "draft",
                    "text": "This late commentary should be dropped.",
                },
            },
        },
        store,
    )

    assert message is None


def test_projector_drops_late_event_after_switching_to_a_different_thread_with_runtime_index() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    first = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="first")
    second = store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="second")
    registry = SessionRegistry(store)
    registry.bind_cwd("demo", "conv-1", first.cwd)
    registry.bind_thread("demo", "conv-1", first.thread_id)
    registry.bind_thread("demo", "conv-1", second.thread_id)
    projector = MessageProjector(session_registry=registry)

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
                    "text": "First thread reply",
                },
            },
        },
        store,
    )

    assert message is None


def test_command_execution_item_is_hidden_when_tool_calls_are_disabled() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
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

    assert message is None


def test_command_execution_item_can_be_projected_when_tool_calls_are_enabled() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_toolcall_visibility("demo", "conv-1", enabled=True)
    projector = MessageProjector()

    progress = projector.project_notification(
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

    assert progress is not None
    assert progress.message_type == "turn_progress"
    assert "Executed `pytest -q`" in progress.text


def test_server_request_resolved_closes_matching_pending_request() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    pending = store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="7",
        kind="approval",
        summary="Run tests",
        payload={"command": "pytest -q"},
        request_id="99",
        request_method="item/commandExecution/requestApproval",
        thread_id="thr_1",
        turn_id="turn_1",
        item_id="cmd_1",
    )
    store.mark_pending_request_submitted("7", {"decision": "accept"})
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "serverRequest/resolved",
            "params": {
                "requestId": "99",
                "threadId": "thr_1",
                "turnId": "turn_1",
                "itemId": "cmd_1",
                "result": {"decision": "accept"},
            },
        },
        store,
    )

    assert message is None
    assert store.get_pending_request(pending.ticket_id) is None
    assert store.get_binding("demo", "conv-1").pending_request_ids == []


def test_file_change_item_is_hidden_when_tool_calls_are_disabled() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "file_1",
                    "type": "fileChange",
                    "changes": [{"path": "src/imcodex/store.py"}],
                },
            },
        },
        store,
    )

    assert message is None


def test_file_change_item_can_be_projected_when_tool_calls_are_enabled() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_toolcall_visibility("demo", "conv-1", enabled=True)
    projector = MessageProjector()

    message = projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {
                    "id": "file_1",
                    "type": "fileChange",
                    "changes": [{"path": "src/imcodex/store.py"}],
                },
            },
        },
        store,
    )

    assert message is not None
    assert message.message_type == "turn_progress"
    assert "Changed files:" in message.text
    assert "src/imcodex/store.py" in message.text


def test_turn_plan_update_respects_commentary_visibility() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    shown = projector.project_notification(
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "explanation": "Plan updated.",
                "plan": [
                    {"step": "Inspect current state", "status": "completed"},
                    {"step": "Implement fix", "status": "inProgress"},
                ],
            },
        },
        store,
    )

    assert shown is not None
    assert shown.message_type == "turn_progress"
    assert "Plan updated." in shown.text
    assert "[completed] Inspect current state" in shown.text
    assert "[inProgress] Implement fix" in shown.text

    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    hidden = projector.project_notification(
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "explanation": "Plan updated.",
                "plan": [
                    {"step": "Inspect current state", "status": "completed"},
                ],
            },
        },
        store,
    )

    assert hidden is None

from __future__ import annotations

from imcodex.models import PendingRequest
from imcodex.bridge import MessageProjector
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


def test_agent_message_delta_can_be_projected_when_commentary_is_visible() -> None:
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

    assert message is not None
    assert message.message_type == "turn_progress"
    assert "Inspecting the active thread binding." in message.text


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


def test_delayed_turn_started_for_older_thread_preserves_pending_new_thread_label() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    old_thread = store.record_thread("thr_old", cwd=r"D:\work\alpha", preview="old")
    new_thread = store.record_thread("thr_new", cwd=r"D:\work\alpha", preview="")
    store.set_active_thread("demo", "conv-1", old_thread.thread_id)
    store.set_active_thread("demo", "conv-1", new_thread.thread_id)
    store.mark_pending_first_thread_label("demo", "conv-1", new_thread.thread_id)
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
    assert store.consume_pending_first_thread_label("demo", "conv-1", new_thread.thread_id) is True


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

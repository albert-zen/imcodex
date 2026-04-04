from __future__ import annotations

from imcodex.models import PendingRequest
from imcodex.projector import MessageProjector
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


def test_turn_completed_message_includes_summary_and_changed_files() -> None:
    projector = MessageProjector()

    message = projector.render_turn_completed(
        final_text="Implemented the webhook bridge.",
        command_summaries=["Executed `pytest -q`"],
        changed_files=["src/imcodex/api.py", "src/imcodex/backend.py"],
        failed=False,
        interrupted=False,
    )

    assert message.message_type == "turn_result"
    assert "Implemented the webhook bridge." in message.text
    assert "src/imcodex/api.py" in message.text
    assert "Executed `pytest -q`" in message.text


def test_project_notification_attaches_turn_completion_to_conversation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    projector = MessageProjector()

    projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {"id": "item_1", "type": "agentMessage", "text": "Hello from Codex"},
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

    assert message is not None
    assert message.channel_id == "demo"
    assert message.conversation_id == "conv-1"
    assert "Hello from Codex" in message.text


def test_turn_completion_still_routes_after_switching_active_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    first = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="first")
    second = store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="second")
    store.set_active_thread("demo", "conv-1", first.thread_id)
    projector = MessageProjector()

    projector.project_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "item": {"id": "item_1", "type": "agentMessage", "text": "First thread reply"},
            },
        },
        store,
    )
    store.set_active_thread("demo", "conv-1", second.thread_id)

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

    assert message is not None
    assert message.channel_id == "demo"
    assert message.conversation_id == "conv-1"

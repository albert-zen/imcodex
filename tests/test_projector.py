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
    assert final.message_type == "turn_progress"
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

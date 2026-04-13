from __future__ import annotations

from imcodex.bridge.request_registry import RequestRegistry
from imcodex.store import ConversationStore


def test_request_registry_tracks_pending_submitted_and_resolved_requests() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = RequestRegistry(store)

    opened = registry.open_request(
        channel_id="qq",
        conversation_id="conv-1",
        native_request_id="native-99",
        request_method="item/commandExecution/requestApproval",
        request_kind="approval",
        summary="Approve shell command",
        payload={"command": "pytest -q"},
        thread_id="thr_1",
        turn_id="turn_1",
        item_id="item_1",
    )

    assert opened.ticket_id == "1"
    assert opened.native_request_id == "native-99"
    assert opened.status == "pending"
    assert store.get_binding("qq", "conv-1").pending_request_ids == ["1"]

    registry.mark_submitted(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        resolution={"decision": "accept"},
    )
    submitted = registry.get_by_ticket("qq", "conv-1", "1")
    assert submitted is not None
    assert submitted.status == "submitted"

    resolved = registry.resolve_native_request(
        native_request_id="native-99",
        resolution={"decision": "accept"},
    )
    assert resolved is not None
    assert resolved.status == "resolved"
    assert registry.get_by_ticket("qq", "conv-1", "1") is None
    assert registry.list_open_requests("qq", "conv-1") == []


def test_request_registry_preserves_open_ended_request_kinds() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = RequestRegistry(store)

    question = registry.open_request(
        channel_id="qq",
        conversation_id="conv-1",
        native_request_id="native-100",
        request_method="item/tool/requestUserInput",
        request_kind="question",
        summary="Additional input required",
        payload={"questions": [{"id": "branch", "question": "Which branch?"}]},
        thread_id="thr_1",
        turn_id="turn_1",
        item_id="item_2",
    )

    assert question.ticket_id == "1"
    assert question.request_kind == "question"
    assert registry.get_by_native_request_id("native-100") is not None
    assert registry.list_open_requests("qq", "conv-1")[0].request_kind == "question"

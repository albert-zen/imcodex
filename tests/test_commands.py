from __future__ import annotations

from imcodex.bridge import CommandRouter
from imcodex.store import ConversationStore


def test_requests_command_uses_native_request_identity() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/requests")

    assert response.action == "requests.list"
    assert "native-request-abcdef" in response.text


def test_approve_without_id_targets_single_pending_request() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/approve")

    assert response.action == "approval.accept"
    assert response.request_id == "native-request-abcdef"


def test_approve_prefix_must_be_unique() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    for suffix in ("111", "222"):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            request_handle=f"native-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
        )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/approve native-request-")

    assert response.action == "approval.accept.missing"
    assert "Ambiguous" in response.text

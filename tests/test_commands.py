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


def test_help_lists_phase_one_commands_and_only_light_native_entry() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/help")

    assert response.action == "help"
    assert "Thread" in response.text
    assert "/config read [key]" in response.text
    assert "/show commentary|toolcalls|system" in response.text
    assert "/native help" in response.text
    assert "/thread fork" not in response.text


def test_show_system_updates_bridge_visibility_only() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/show system")

    assert response.action == "settings.visibility"
    assert store.get_binding("qq", "conv-1").show_system is True


def test_native_help_exposes_advanced_escape_hatch_commands() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/native help")

    assert response.action == "native.help"
    assert "/native call <method> <json>" in response.text
    assert "/native requests" in response.text

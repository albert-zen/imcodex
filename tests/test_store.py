from __future__ import annotations

import json

from imcodex.store import ConversationStore


def test_store_persists_only_minimal_native_first_state(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.set_commentary_visibility("qq", "conv-1", enabled=False)
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

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["version"] == 2
    assert payload["bindings"][0]["thread_id"] == "thr_1"
    assert "active_turn_id" not in payload["bindings"][0]
    assert payload["pending_requests"][0]["request_id"] == "native-request-abcdef"
    assert "summary" not in payload["pending_requests"][0]


def test_store_ignores_legacy_state_file_shape(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"bindings":[{"channel_id":"qq"}]}', encoding="utf-8")

    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)

    binding = store.get_binding("qq", "conv-1")
    assert binding.thread_id is None


def test_store_matches_unique_request_prefix() -> None:
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

    matched = store.match_pending_request("qq", "conv-1", "native-request-a")

    assert matched is not None
    assert matched.request_id == "native-request-abcdef"


def test_binding_a_thread_moves_ownership_to_latest_conversation() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    store.bind_thread("qq", "old-conv", "thr_1")
    store.bind_thread("qq", "new-conv", "thr_1")

    binding = store.find_binding_by_thread_id("thr_1")
    assert binding is not None
    assert binding.conversation_id == "new-conv"


def test_binding_a_thread_moves_pending_requests_to_latest_conversation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "old-conv", "thr_1")
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="old-conv",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )

    store.bind_thread("qq", "new-conv", "thr_1")

    assert store.match_pending_request("qq", "new-conv", "native-request-abcdef") is not None
    assert store.match_pending_request("qq", "old-conv", "native-request-abcdef") is None


def test_visibility_preferences_persist_without_thread_or_cwd(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)

    store.set_visibility_profile("qq", "conv-1", "minimal")

    reloaded = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    binding = reloaded.get_binding("qq", "conv-1")
    assert binding.visibility_profile == "minimal"
    assert binding.show_commentary is False
    assert binding.show_toolcalls is False

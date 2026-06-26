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
    assert payload["pending_requests"] == []


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
    assert binding.show_system is False


def test_verbose_visibility_profile_enables_all_im_toggles(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)

    store.set_visibility_profile("qq", "conv-1", "verbose")

    reloaded = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    binding = reloaded.get_binding("qq", "conv-1")
    assert binding.visibility_profile == "verbose"
    assert binding.show_commentary is True
    assert binding.show_toolcalls is True
    assert binding.show_system is True


def test_thread_browser_context_is_runtime_only_and_expires(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    now = {"value": 100.0}
    store = ConversationStore(clock=lambda: now["value"], state_path=state_path)

    store.set_thread_browser_context(
        "qq",
        "conv-1",
        thread_ids=["thr_1", "thr_2"],
        page=1,
        total=2,
        query="alpha",
        ttl_s=30.0,
    )

    context = store.get_thread_browser_context("qq", "conv-1")
    assert context is not None
    assert context.thread_ids == ["thr_1", "thr_2"]

    reloaded = ConversationStore(clock=lambda: now["value"], state_path=state_path)
    assert reloaded.get_thread_browser_context("qq", "conv-1") is None

    now["value"] = 131.0
    assert store.get_thread_browser_context("qq", "conv-1") is None


def test_native_appserver_journal_is_bounded_and_runtime_only(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 10.0, state_path=state_path, native_event_journal_limit=2)

    store.append_native_appserver_event(
        seen_at=10.0,
        direction="notification",
        method="turn/started",
        category="turn",
        kind="turn_started",
        thread_id="thr_1",
        turn_id="turn_1",
        summary={"payload_keys": ["threadId", "turn"]},
    )
    store.append_native_appserver_event(
        seen_at=11.0,
        direction="notification",
        method="item/completed",
        category="item",
        kind="item_completed",
        thread_id="thr_1",
        turn_id="turn_1",
        item_id="item_1",
        summary={"item_type": "agentMessage"},
    )
    rejected = store.append_native_appserver_event(
        seen_at=12.0,
        direction="server_request",
        method="item/tool/call",
        category="item",
        kind="unknown",
        thread_id="thr_1",
        turn_id="turn_1",
        request_id="native-request-tool",
        outcome="rejected",
        summary={"payload_key_count": 6},
    )

    assert [entry.method for entry in store.list_native_appserver_events()] == [
        "item/completed",
        "item/tool/call",
    ]
    assert store.list_native_appserver_events(limit=1) == [rejected]

    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert "native_appserver_journal" not in payload

    reloaded = ConversationStore(clock=lambda: 12.0, state_path=state_path)
    assert reloaded.list_native_appserver_events() == []


def test_store_has_no_bridge_owned_next_model_override_state() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    assert not hasattr(store, "_next_model_overrides")
    assert not hasattr(store, "set_next_model_override")
    assert not hasattr(store, "pop_next_model_override")

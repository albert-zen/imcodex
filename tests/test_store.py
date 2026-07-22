from __future__ import annotations

import json

import pytest

from imcodex.store import ConversationStore


@pytest.mark.asyncio
async def test_store_persists_only_minimal_native_first_state(tmp_path) -> None:
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
    await store.flush_pending_writes()

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["version"] == 2
    assert payload["bindings"][0]["thread_id"] == "thr_1"
    assert "active_turn_id" not in payload["bindings"][0]
    assert payload["pending_requests"] == []
    assert "native_thread_tool_thread_ids" not in payload
    assert payload["pending_terminal_deliveries"] == []

    reloaded = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    assert reloaded.get_binding("qq", "conv-1").thread_id == "thr_1"


def test_store_persists_terminal_delivery_checkpoint_without_persisting_active_turn(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 7.0, state_path=state_path)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")

    reloaded = ConversationStore(clock=lambda: 8.0, state_path=state_path)

    assert reloaded.get_active_turn("thr_1") is None
    pending = reloaded.list_pending_terminal_deliveries("thr_1")
    assert [(item.thread_id, item.turn_id, item.message) for item in pending] == [
        ("thr_1", "turn_1", None)
    ]


def test_store_persists_staged_terminal_message_until_delivery_ack(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 7.0, state_path=state_path)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.stage_terminal_delivery(
        thread_id="thr_1",
        turn_id="turn_1",
        message={
            "channel_id": "qq",
            "conversation_id": "conv-1",
            "message_type": "turn_result",
            "text": "Recovered result",
            "request_id": None,
            "metadata": {"delivery_id": "stable-1"},
        },
    )

    reloaded = ConversationStore(clock=lambda: 8.0, state_path=state_path)
    pending = reloaded.list_pending_terminal_deliveries()
    assert pending[0].message is not None
    assert pending[0].message["metadata"]["delivery_id"] == "stable-1"

    reloaded.stage_terminal_delivery(
        thread_id="thr_1",
        turn_id="turn_1",
        message={
            "channel_id": "qq",
            "conversation_id": "different-route",
            "message_type": "turn_result",
            "text": "Replayed fallback",
            "request_id": None,
            "metadata": {"delivery_id": "different"},
        },
    )
    unchanged = reloaded.list_pending_terminal_deliveries()[0]
    assert unchanged.message is not None
    assert unchanged.message["text"] == "Recovered result"
    assert unchanged.message["conversation_id"] == "conv-1"

    reloaded.update_terminal_delivery_message(
        "thr_1",
        "turn_1",
        {
            **unchanged.message,
            "text": "Recovered result with durable delivery progress",
        },
    )
    updated = ConversationStore(clock=lambda: 8.5, state_path=state_path)
    assert updated.list_pending_terminal_deliveries()[0].message["text"] == (
        "Recovered result with durable delivery progress"
    )

    updated.complete_terminal_delivery("thr_1", "turn_1")
    assert ConversationStore(clock=lambda: 9.0, state_path=state_path).list_pending_terminal_deliveries() == []


def test_suppressing_projection_does_not_consume_terminal_delivery_checkpoint() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")

    store.suppress_turn("thr_1", "turn_1")

    assert store.is_turn_suppressed("thr_1", "turn_1") is True
    assert [item.turn_id for item in store.list_pending_terminal_deliveries()] == ["turn_1"]


def test_clearing_stale_binding_preserves_staged_delivery_but_drops_unprojected_watch() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.stage_terminal_delivery(
        thread_id="thr_1",
        turn_id="turn_staged",
        message={
            "channel_id": "qq",
            "conversation_id": "conv-1",
            "message_type": "turn_result",
            "text": "Already projected",
            "request_id": None,
            "metadata": {},
        },
    )
    store.watch_terminal_delivery("thr_1", "turn_watched")

    store.clear_thread_binding("qq", "conv-1")

    pending = store.list_pending_terminal_deliveries()
    assert [(item.turn_id, item.message is not None) for item in pending] == [
        ("turn_staged", True)
    ]


def test_discard_terminal_watch_never_removes_staged_message() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.watch_terminal_delivery("thr_1", "turn_watch")
    store.stage_terminal_delivery(
        thread_id="thr_1",
        turn_id="turn_staged",
        message={
            "channel_id": "qq",
            "conversation_id": "conv-1",
            "message_type": "turn_result",
            "text": "Owed",
            "request_id": None,
            "metadata": {},
        },
    )

    store.discard_terminal_watch("thr_1", "turn_watch")
    store.discard_terminal_watch("thr_1", "turn_staged")

    assert [item.turn_id for item in store.list_pending_terminal_deliveries()] == [
        "turn_staged"
    ]


def test_store_fails_explicitly_on_legacy_or_corrupt_state(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"bindings":[{"channel_id":"qq"}]}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unsupported or invalid bridge state"):
        ConversationStore(clock=lambda: 1.0, state_path=state_path)

    state_path.write_text("{truncated", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Could not load bridge state"):
        ConversationStore(clock=lambda: 1.0, state_path=state_path)


def test_store_atomic_save_preserves_previous_state_on_replace_failure(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.set_bootstrap_cwd("qq", "conv-1", "/first")
    previous = state_path.read_text(encoding="utf-8")

    def fail_replace(_source, _target) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr("imcodex.store.os.replace", fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        store.set_bootstrap_cwd("qq", "conv-1", "/second")

    assert state_path.read_text(encoding="utf-8") == previous
    assert not state_path.with_suffix(".json.tmp").exists()


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


def test_store_bounds_persisted_inbound_dedupe_horizon() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    for index in range(1100):
        store.mark_inbound_message_processed(
            channel_id="gateway",
            conversation_id="conv-1",
            user_id="u1",
            message_id=f"m{index}",
            text_fingerprint="fingerprint",
        )

    recent = store.get_binding("gateway", "conv-1").reply_context["recent_inbound_message_ids"]
    assert len(recent) == store.RECENT_INBOUND_MESSAGE_ID_LIMIT
    assert recent[0] == "m76"
    assert recent[-1] == "m1099"


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
        all_thread_ids=["thr_1", "thr_2", "thr_3"],
        project_paths=[r"D:\work\alpha", r"D:\work\beta"],
        project_path=r"D:\work\alpha",
        ttl_s=30.0,
    )

    context = store.get_thread_browser_context("qq", "conv-1")
    assert context is not None
    assert context.thread_ids == ["thr_1", "thr_2"]
    assert context.all_thread_ids == ["thr_1", "thr_2", "thr_3"]
    assert context.project_paths == [r"D:\work\alpha", r"D:\work\beta"]
    assert context.project_path == r"D:\work\alpha"

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

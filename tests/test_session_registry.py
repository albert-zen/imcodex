from __future__ import annotations

from pathlib import Path

from imcodex.bridge.session_registry import SessionRegistry
from imcodex.store import ConversationStore


def test_session_registry_persists_selected_cwd_and_thread_binding(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)
    registry = SessionRegistry(store)

    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    store.record_thread(
        "thr_1",
        cwd=r"D:\work\alpha",
        preview="Investigate the Windows restart issue",
        name="Windows restart issue",
        path=r"D:\work\alpha",
        status="inProgress",
    )
    registry.bind_thread("qq", "conv-1", "thr_1")

    reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)
    session = SessionRegistry(reloaded).get("qq", "conv-1")

    assert session.selected_cwd == r"D:\work\alpha"
    assert session.thread_id == "thr_1"
    assert session.last_seen_thread_name is None
    assert session.last_seen_thread_path is None
    assert session.last_seen_thread_status is None


def test_session_registry_reflects_turn_and_pending_ticket_state() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.note_turn_started("thr_1", "turn_1", "inProgress")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="approval",
        summary="Approve shell command",
        payload={"command": "pytest -q"},
        request_id="native-1",
        thread_id="thr_1",
        turn_id="turn_1",
    )

    session = registry.get("qq", "conv-1")

    assert session.thread_id == "thr_1"
    assert session.active_turn_id == "turn_1"
    assert session.active_turn_status == "inProgress"
    assert session.pending_request_ids == ["1"]


def test_session_registry_preserves_last_seen_thread_status_when_rebinding() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha", status="idle")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.note_turn_started("thr_1", "turn_1", "inProgress")

    store.clear_active_thread("qq", "conv-1")
    registry.bind_thread("qq", "conv-1", "thr_1")
    session = registry.get("qq", "conv-1")

    assert session.last_seen_thread_status == "inProgress"


def test_session_registry_tracks_latest_runtime_binding_for_thread_id() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    session = registry.get_by_thread("thr_1")

    assert session is not None
    assert session.channel_id == "qq"
    assert session.conversation_id == "conv-2"
    assert session.thread_id == "thr_1"


def test_turn_started_keeps_latest_runtime_owner_for_shared_thread() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    session = registry.note_turn_started("thr_1", "turn_1", "inProgress")

    assert session is not None
    assert session.conversation_id == "conv-2"
    assert session.active_turn_id == "turn_1"
    assert store.get_binding("qq", "conv-1").active_turn_id is None
    assert registry.get_by_thread("thr_1").conversation_id == "conv-2"


def test_sync_clears_runtime_mapping_when_session_detaches_thread() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    store.clear_active_thread("qq", "conv-2")
    registry.sync("qq", "conv-2")

    assert registry.find_binding("thr_1") is None
    assert registry.get_by_thread("thr_1") is None


def test_bind_runtime_keeps_existing_pending_requests_with_original_owner() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        request_id="native-1",
        thread_id="thr_1",
        turn_id="turn_1",
    )

    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    original = store.list_pending_requests("qq", "conv-1")
    latest = store.list_pending_requests("qq", "conv-2")
    assert [request.ticket_id for request in original] == ["1"]
    assert latest == []


def test_bind_runtime_clears_stale_active_turn_from_previous_owner() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.note_turn_started("thr_1", "turn_1", "inProgress")

    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")
    registry.note_turn_completed("thr_1", "turn_1", "completed")

    prior = store.get_binding("qq", "conv-1")
    latest = store.get_binding("qq", "conv-2")
    assert prior.active_thread_id is None
    assert prior.active_turn_id is None
    assert prior.active_turn_status is None
    assert latest.active_turn_id is None
    assert latest.active_turn_status == "completed"


def test_bind_runtime_does_not_renumber_existing_tickets_on_rebind() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="approval",
        summary="Moved request",
        payload={"command": "pytest -q"},
        request_id="native-1",
        thread_id="thr_1",
        turn_id="turn_1",
    )

    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-2",
        ticket_id="1",
        kind="approval",
        summary="Existing request",
        payload={"command": "python -V"},
        request_id="native-2",
        thread_id="thr_other",
        turn_id="turn_2",
    )

    registry.bind_thread("qq", "conv-2", "thr_1")

    original = store.list_pending_requests("qq", "conv-1")
    latest = store.list_pending_requests("qq", "conv-2")
    assert [request.summary for request in original] == ["Moved request"]
    assert [request.summary for request in latest] == ["Existing request"]
    assert [request.ticket_id for request in latest] == ["1"]


def test_bind_runtime_keeps_destination_next_ticket_local_to_destination() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="7",
        kind="approval",
        summary="Moved request",
        payload={"command": "pytest -q"},
        request_id="native-7",
        thread_id="thr_1",
        turn_id="turn_7",
    )

    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    assert store.next_ticket_id("qq", "conv-2") == "1"


def test_find_binding_prefers_active_owner_after_runtime_cache_is_rebuilt() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.bind_cwd("qq", "conv-2", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-2", "thr_1")

    reloaded_registry = SessionRegistry(store)
    binding = reloaded_registry.find_binding("thr_1")

    assert binding is not None
    assert binding.conversation_id == "conv-2"


def test_get_by_thread_returns_none_when_thread_is_not_the_current_active_owner() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="beta")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_2")

    assert registry.find_binding("thr_1") is None
    assert registry.get_by_thread("thr_1") is None


def test_late_turn_started_does_not_reattach_recovered_thread_from_stale_runtime_cache() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")

    store.clear_active_thread("qq", "conv-1")
    session = registry.note_turn_started("thr_1", "turn_1", "inProgress")
    binding = store.get_binding("qq", "conv-1")

    assert session is None
    assert binding.active_thread_id is None
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None


def test_late_turn_completed_does_not_keep_stale_runtime_owner_after_recover() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.note_turn_started("thr_1", "turn_1", "inProgress")

    store.clear_active_thread("qq", "conv-1")
    session = registry.note_turn_completed("thr_1", "turn_1", "completed")
    binding = store.get_binding("qq", "conv-1")

    assert session is None
    assert binding.active_thread_id is None
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None
    assert registry.find_binding("thr_1") is None


def test_turn_started_keeps_binding_even_when_thread_snapshot_has_not_been_rehydrated() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    binding = store.get_binding("qq", "conv-1")
    binding.active_thread_id = "thr_1"
    registry.sync("qq", "conv-1")

    session = registry.note_turn_started("thr_1", "turn_1", "inProgress")

    assert session is not None
    assert session.thread_id == "thr_1"
    assert session.active_turn_id == "turn_1"
    assert session.active_turn_status == "inProgress"


def test_find_routing_binding_returns_none_after_switching_to_a_different_thread() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    registry = SessionRegistry(store)

    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="beta")
    registry.bind_cwd("qq", "conv-1", r"D:\work\alpha")
    registry.bind_thread("qq", "conv-1", "thr_1")
    registry.bind_thread("qq", "conv-1", "thr_2")

    binding = registry.find_routing_binding("thr_1")

    assert binding is None

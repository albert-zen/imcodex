from __future__ import annotations

from pathlib import Path

from imcodex.bridge.session_registry import SessionRegistry
from imcodex.store import ConversationStore


def test_session_registry_persists_selected_cwd_and_thread_snapshot(tmp_path: Path) -> None:
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
    assert session.last_seen_thread_name == "Windows restart issue"
    assert session.last_seen_thread_path == r"D:\work\alpha"
    assert session.last_seen_thread_status == "inProgress"


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

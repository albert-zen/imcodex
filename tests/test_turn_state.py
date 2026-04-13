from __future__ import annotations

from imcodex.bridge.turn_state import TurnStateMachine


def test_turn_state_marks_older_turns_as_stale_when_new_turn_starts() -> None:
    machine = TurnStateMachine()

    first = machine.start("thr_1", "turn_1")
    assert first.status == "starting"
    machine.mark_in_progress("thr_1", "turn_1")
    second = machine.start("thr_1", "turn_2")

    assert second.status == "starting"
    assert machine.is_current("thr_1", "turn_2") is True
    assert machine.is_stale("thr_1", "turn_1") is True


def test_turn_state_tracks_pending_requests_and_terminal_emission() -> None:
    machine = TurnStateMachine()

    machine.start("thr_1", "turn_1")
    machine.mark_in_progress("thr_1", "turn_1")
    machine.await_approval("thr_1", "turn_1", "native-1")
    current = machine.get("thr_1")
    assert current is not None
    assert current.status == "awaiting_approval"
    assert current.pending_request_ids == ["native-1"]

    machine.resolve_request("thr_1", "turn_1", "native-1")
    current = machine.get("thr_1")
    assert current is not None
    assert current.status == "in_progress"

    assert machine.can_emit_terminal("thr_1", "turn_1") is True
    machine.mark_terminal_emitted("thr_1", "turn_1")
    assert machine.can_emit_terminal("thr_1", "turn_1") is False

    machine.mark_completed("thr_1", "turn_1")
    assert machine.get("thr_1").status == "completed"

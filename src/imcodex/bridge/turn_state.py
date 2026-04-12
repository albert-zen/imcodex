from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnStateRecord:
    thread_id: str
    turn_id: str
    status: str
    pending_request_ids: list[str] = field(default_factory=list)
    terminal_emitted: bool = False


class TurnStateMachine:
    def __init__(self) -> None:
        self._active: dict[str, TurnStateRecord] = {}

    def start(self, thread_id: str, turn_id: str) -> TurnStateRecord:
        record = TurnStateRecord(thread_id=thread_id, turn_id=turn_id, status="starting")
        self._active[thread_id] = record
        return record

    def get(self, thread_id: str) -> TurnStateRecord | None:
        return self._active.get(thread_id)

    def is_current(self, thread_id: str, turn_id: str) -> bool:
        current = self.get(thread_id)
        return current is not None and current.turn_id == turn_id

    def is_stale(self, thread_id: str, turn_id: str) -> bool:
        current = self.get(thread_id)
        return current is not None and current.turn_id != turn_id

    def mark_in_progress(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        return self._set_status(thread_id, turn_id, "in_progress")

    def await_approval(self, thread_id: str, turn_id: str, request_id: str) -> TurnStateRecord | None:
        record = self._set_status(thread_id, turn_id, "awaiting_approval")
        if record is not None and request_id not in record.pending_request_ids:
            record.pending_request_ids.append(request_id)
        return record

    def await_user_input(self, thread_id: str, turn_id: str, request_id: str) -> TurnStateRecord | None:
        record = self._set_status(thread_id, turn_id, "awaiting_user_input")
        if record is not None and request_id not in record.pending_request_ids:
            record.pending_request_ids.append(request_id)
        return record

    def resolve_request(self, thread_id: str, turn_id: str, request_id: str) -> TurnStateRecord | None:
        record = self.get(thread_id)
        if record is None or record.turn_id != turn_id:
            return None
        if request_id in record.pending_request_ids:
            record.pending_request_ids.remove(request_id)
        if not record.pending_request_ids and record.status in {"awaiting_approval", "awaiting_user_input"}:
            record.status = "in_progress"
        return record

    def interrupt(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        return self._set_status(thread_id, turn_id, "interrupting")

    def mark_completed(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        return self._set_status(thread_id, turn_id, "completed")

    def mark_failed(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        return self._set_status(thread_id, turn_id, "failed")

    def mark_interrupted(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        return self._set_status(thread_id, turn_id, "interrupted")

    def can_emit_terminal(self, thread_id: str, turn_id: str) -> bool:
        record = self.get(thread_id)
        return record is not None and record.turn_id == turn_id and not record.terminal_emitted

    def mark_terminal_emitted(self, thread_id: str, turn_id: str) -> TurnStateRecord | None:
        record = self.get(thread_id)
        if record is None or record.turn_id != turn_id:
            return None
        record.terminal_emitted = True
        return record

    def _set_status(self, thread_id: str, turn_id: str, status: str) -> TurnStateRecord | None:
        record = self.get(thread_id)
        if record is None or record.turn_id != turn_id:
            return None
        record.status = status
        return record

from __future__ import annotations

from dataclasses import dataclass

from ..models import NativeThreadSnapshot


ACTIVE_THREAD_STATUSES = {"active", "inprogress", "in_progress", "running", "working"}


class StaleThreadBindingError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"thread binding is stale: {thread_id}")


class ThreadSelectionError(RuntimeError):
    pass


@dataclass(slots=True)
class TurnSubmission:
    kind: str
    thread_id: str
    turn_id: str


@dataclass(slots=True)
class ThreadListResult:
    threads: list[NativeThreadSnapshot]
    next_cursor: str | None = None

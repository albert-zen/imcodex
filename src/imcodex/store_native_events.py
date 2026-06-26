from __future__ import annotations

from .models import NativeAppServerJournalEntry


DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT = 50


class NativeEventJournalMixin:
    def append_native_appserver_event(
        self,
        *,
        seen_at: float,
        direction: str,
        method: str,
        category: str,
        kind: str,
        thread_id: str = "",
        turn_id: str = "",
        item_id: str = "",
        request_id: str | None = None,
        outcome: str | None = None,
        note: str | None = None,
        summary: dict | None = None,
    ) -> NativeAppServerJournalEntry:
        self._native_appserver_journal_sequence += 1
        entry = NativeAppServerJournalEntry(
            sequence=self._native_appserver_journal_sequence,
            seen_at=seen_at,
            direction=direction,
            method=method,
            category=category,
            kind=kind,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            request_id=request_id,
            outcome=outcome,
            note=note,
            summary=dict(summary or {}),
        )
        self._native_appserver_journal.append(entry)
        return entry

    def update_native_appserver_event(
        self,
        sequence: int,
        *,
        outcome: str | None = None,
        note: str | None = None,
    ) -> NativeAppServerJournalEntry | None:
        for entry in reversed(self._native_appserver_journal):
            if entry.sequence != sequence:
                continue
            if outcome is not None:
                entry.outcome = outcome
            if note is not None:
                entry.note = note
            return entry
        return None

    def list_native_appserver_events(
        self,
        *,
        limit: int = DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT,
    ) -> list[NativeAppServerJournalEntry]:
        if limit <= 0:
            return []
        return list(self._native_appserver_journal)[-limit:]

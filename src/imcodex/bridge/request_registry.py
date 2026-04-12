from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..store import ConversationStore


@dataclass(slots=True)
class RequestRecord:
    native_request_id: str | None
    ticket_id: str
    channel_id: str
    conversation_id: str
    thread_id: str | None
    turn_id: str | None
    item_id: str | None
    request_method: str | None
    request_kind: str
    summary: str
    created_at: float
    submitted_at: float | None
    resolved_at: float | None
    status: str
    payload: dict[str, Any]

    @property
    def kind(self) -> str:
        return self.request_kind

    @property
    def request_id(self) -> str | None:
        return self.native_request_id


class RequestRegistry:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store

    def open_request(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        native_request_id: str | None,
        request_method: str | None,
        request_kind: str,
        summary: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
    ) -> RequestRecord:
        ticket_id = self.store.next_ticket_id(channel_id, conversation_id)
        pending = self.store.create_pending_request(
            channel_id=channel_id,
            conversation_id=conversation_id,
            ticket_id=ticket_id,
            kind=request_kind,
            summary=summary,
            payload=payload,
            request_id=native_request_id,
            request_method=request_method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
        )
        return self._to_record(pending)

    def get_by_ticket(
        self,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
    ) -> RequestRecord | None:
        pending = self.store.get_pending_request(
            ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if pending is None:
            return None
        return self._to_record(pending)

    def get_by_native_request_id(self, native_request_id: str) -> RequestRecord | None:
        pending = self.store.get_pending_request_by_request_id(native_request_id)
        if pending is None:
            return None
        return self._to_record(pending)

    def list_open_requests(self, channel_id: str, conversation_id: str) -> list[RequestRecord]:
        return [
            self._to_record(request)
            for request in self.store.list_pending_requests(channel_id, conversation_id)
        ]

    def mark_submitted(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
        resolution: dict[str, Any],
    ) -> RequestRecord | None:
        pending = self.store.mark_pending_request_submitted(
            ticket_id,
            resolution,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if pending is None:
            return None
        return self._to_record(pending)

    def resolve_native_request(
        self,
        *,
        native_request_id: str,
        resolution: dict[str, Any],
    ) -> RequestRecord | None:
        pending = self.store.get_pending_request_by_request_id(native_request_id)
        if pending is None:
            return None
        resolved = self.store.resolve_pending_request(
            pending.ticket_id,
            resolution,
            channel_id=pending.channel_id,
            conversation_id=pending.conversation_id,
        )
        if resolved is None:
            return None
        return self._to_record(resolved)

    def _to_record(self, pending) -> RequestRecord:
        return RequestRecord(
            native_request_id=pending.request_id,
            ticket_id=pending.ticket_id,
            channel_id=pending.channel_id,
            conversation_id=pending.conversation_id,
            thread_id=pending.thread_id,
            turn_id=pending.turn_id,
            item_id=pending.item_id,
            request_method=pending.request_method,
            request_kind=pending.kind,
            summary=pending.summary,
            created_at=pending.created_at,
            submitted_at=pending.submitted_at,
            resolved_at=pending.resolved_at,
            status=pending.status,
            payload=dict(pending.payload),
        )

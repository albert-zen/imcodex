from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ThreadRecord:
    thread_id: str
    preview: str
    status: str
    last_used_at: float
    cwd: str
    name: str | None = None
    path: str | None = None
    last_turn_id: str | None = None
    last_turn_status: str | None = None
    stale_turn_ids: list[str] = field(default_factory=list)
    created_seq: int = 0


@dataclass(slots=True)
class ConversationBinding:
    channel_id: str
    conversation_id: str
    selected_cwd: str | None = None
    selected_model: str | None = None
    active_thread_id: str | None = None
    active_turn_id: str | None = None
    active_turn_status: str | None = None
    last_inbound_message_id: str | None = None
    pending_request_ids: list[str] = field(default_factory=list)
    next_ticket: int = 1
    known_thread_ids: list[str] = field(default_factory=list)
    permission_profile: str = "review"
    visibility_profile: str = "standard"
    show_commentary: bool = True
    show_toolcalls: bool = False
    last_seen_thread_name: str | None = None
    last_seen_thread_path: str | None = None
    last_seen_thread_status: str | None = None


@dataclass(slots=True)
class PendingRequest:
    ticket_id: str
    channel_id: str
    conversation_id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    created_at: float
    request_id: str | None = None
    request_method: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    status: str = "pending"
    submitted_at: float | None = None
    submitted_resolution: dict[str, Any] | None = None
    resolved_at: float | None = None
    resolution: dict[str, Any] | None = None


@dataclass(slots=True)
class InboundMessage:
    channel_id: str
    conversation_id: str
    user_id: str
    message_id: str
    text: str
    reply_to_message_id: str | None = None
    sent_at: str | None = None


@dataclass(slots=True)
class OutboundMessage:
    channel_id: str
    conversation_id: str
    message_type: str
    text: str
    ticket_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ConversationBinding:
    channel_id: str
    conversation_id: str
    thread_id: str | None = None
    bootstrap_cwd: str | None = None
    visibility_profile: str = "standard"
    show_commentary: bool = True
    show_toolcalls: bool = False
    show_system: bool = False
    reply_context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PendingNativeRequestRoute:
    request_id: str
    request_handle: str | None
    channel_id: str
    conversation_id: str
    thread_id: str | None
    turn_id: str | None
    kind: str
    request_method: str | None
    transport_request_id: str | int | None = None
    connection_epoch: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NativeThreadSnapshot:
    thread_id: str
    cwd: str
    preview: str
    status: str
    name: str | None = None
    path: str | None = None
    source: str | None = None


@dataclass(slots=True)
class ThreadBrowserContext:
    channel_id: str
    conversation_id: str
    thread_ids: list[str]
    page: int
    total: int
    query: str | None = None
    include_all: bool = False
    expires_at: float = 0.0


@dataclass(slots=True)
class InboundMessage:
    channel_id: str
    conversation_id: str
    user_id: str
    message_id: str
    text: str
    reply_to_message_id: str | None = None
    sent_at: str | None = None
    trace_id: str | None = None


@dataclass(slots=True)
class OutboundMessage:
    channel_id: str
    conversation_id: str
    message_type: str
    text: str
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

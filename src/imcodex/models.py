from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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
class TerminalDeliveryWatch:
    """A native turn that must be reconciled after transport recovery."""

    thread_id: str
    turn_id: str
    created_at: float = 0.0


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIdentity:
    """Stable identity and native context for one projected IM message."""

    delivery_id: str
    thread_id: str
    turn_id: str


@dataclass(slots=True)
class PendingTerminalDelivery:
    """One projected IM message that remains owed to its destination.

    A native turn may produce more than one answer segment. ``delivery_id`` is
    therefore the durable identity; ``thread_id`` and ``turn_id`` are optional
    native context, not the outbox key. Standalone channel delivery leaves both
    empty.
    """

    delivery_id: str
    thread_id: str
    turn_id: str
    message: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    sequence: int = 0


@dataclass(slots=True)
class NativeThreadSnapshot:
    thread_id: str
    cwd: str
    preview: str
    status: str
    name: str | None = None
    path: str | None = None
    source: str | None = None
    updated_at: float | None = None
    pinned: bool | None = None


@dataclass(slots=True)
class ThreadBrowserContext:
    channel_id: str
    conversation_id: str
    thread_ids: list[str]
    page: int
    total: int
    query: str | None = None
    all_thread_ids: list[str] = field(default_factory=list)
    project_paths: list[str] = field(default_factory=list)
    project_path: str | None = None
    expires_at: float = 0.0


@dataclass(slots=True)
class NativeAppServerJournalEntry:
    sequence: int
    seen_at: float
    direction: str
    method: str
    category: str
    kind: str
    summary: dict[str, Any] = field(default_factory=dict)
    thread_id: str = ""
    turn_id: str = ""
    item_id: str = ""
    request_id: str | None = None
    outcome: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    kind: Literal["image", "file"]
    content_type: str
    local_path: str
    size_bytes: int
    filename: str = ""
    source_channel_id: str = ""
    source_message_id: str = ""


@dataclass(frozen=True, slots=True)
class InboundQuoteAttachment:
    kind: Literal["image", "voice", "video", "file", "attachment"]
    filename: str | None = None
    transcript: str | None = None


@dataclass(frozen=True, slots=True)
class InboundQuote:
    reference_id: str | None = None
    text: str = ""
    attachments: tuple[InboundQuoteAttachment, ...] = ()


@dataclass(slots=True)
class InboundMessage:
    channel_id: str
    conversation_id: str
    user_id: str
    message_id: str
    text: str
    attachments: tuple[InboundAttachment, ...] = ()
    quote: InboundQuote | None = None
    input_error: str | None = None
    reply_to_message_id: str | None = None
    sent_at: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundArtifact:
    kind: Literal["image", "file"]
    local_path: str
    content_type: str
    filename: str
    size_bytes: int
    sha256: str = ""


@dataclass(slots=True)
class OutboundMessage:
    channel_id: str
    conversation_id: str
    message_type: str
    text: str
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[OutboundArtifact] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.artifacts = [
            item if isinstance(item, OutboundArtifact) else OutboundArtifact(**item)
            for item in self.artifacts
        ]

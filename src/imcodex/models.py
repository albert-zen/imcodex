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
class LegacyPendingDeliveryEvidence:
    """Read-only evidence imported from the retired product outbox."""

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
    attachment_id: str = ""


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

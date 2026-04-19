from __future__ import annotations

import hashlib
import re

from ..models import InboundMessage


_WHITESPACE_RE = re.compile(r"\s+")


def ensure_trace_id(message: InboundMessage) -> str:
    if message.trace_id:
        return message.trace_id
    seed = "|".join(
        [
            message.channel_id,
            message.conversation_id,
            message.user_id,
            message.message_id,
        ]
    )
    message.trace_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return message.trace_id


def text_preview(text: str, *, limit: int = 120) -> str:
    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "..."


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

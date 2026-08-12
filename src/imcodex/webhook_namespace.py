from __future__ import annotations

import base64


WEBHOOK_CHANNEL_INSTANCE_ID = "webhook"


def encode_webhook_conversation(channel_id: str, conversation_id: str) -> str:
    """Encode product routing in the SDK Channel's native conversation ID."""

    raw = f"{channel_id}\x00{conversation_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_webhook_conversation(value: str) -> tuple[str, str]:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid webhook conversation namespace") from exc
    channel_id, separator, conversation_id = decoded.partition("\x00")
    if not separator or not channel_id or not conversation_id:
        raise ValueError("invalid webhook conversation namespace")
    return channel_id, conversation_id

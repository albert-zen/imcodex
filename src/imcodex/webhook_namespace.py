from __future__ import annotations

import base64
import json


WEBHOOK_CHANNEL_INSTANCE_ID = "webhook"


def encode_webhook_conversation(channel_id: str, conversation_id: str) -> str:
    payload = json.dumps([channel_id, conversation_id], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"v1:{encoded}"


def decode_webhook_conversation(value: str) -> tuple[str, str]:
    if not value.startswith("v1:"):
        raise ValueError("invalid webhook Conversation identity")
    encoded = value[3:]
    encoded += "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded).decode())
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("invalid webhook Conversation identity")
    return str(payload[0]), str(payload[1])

from __future__ import annotations

import hashlib
import json

from ..models import OutboundMessage, TerminalDeliveryIdentity


def stable_terminal_item_delivery_id(
    *,
    thread_id: str,
    turn_id: str,
    item_id: str,
) -> str:
    return _stable_delivery_id("terminal-item", thread_id, turn_id, item_id)


def stable_terminal_turn_delivery_id(
    *,
    thread_id: str,
    turn_id: str,
) -> str:
    return _stable_delivery_id("terminal-turn", thread_id, turn_id)


def event_terminal_key(event) -> tuple[str, str] | None:
    turn_id = event.turn_id
    if not turn_id and isinstance(event.payload.get("turn"), dict):
        turn_id = str(
            event.payload["turn"].get("id")
            or event.payload["turn"].get("turnId")
            or ""
        )
    if not event.thread_id or not turn_id:
        return None
    return event.thread_id, turn_id


def event_terminal_identity(event) -> TerminalDeliveryIdentity | None:
    terminal_key = event_terminal_key(event)
    if terminal_key is None:
        return None
    if event.kind == "turn_completed":
        return TerminalDeliveryIdentity(
            delivery_id=stable_terminal_turn_delivery_id(
                thread_id=terminal_key[0],
                turn_id=terminal_key[1],
            ),
            thread_id=terminal_key[0],
            turn_id=terminal_key[1],
        )
    if event.kind != "item_completed":
        return None
    item = event.payload.get("item")
    if not isinstance(item, dict):
        return None
    if (
        item.get("type") != "agentMessage"
        or item.get("phase") != "final_answer"
        or not str(item.get("text") or "").strip()
    ):
        return None
    item_id = terminal_item_key(item, fallback_item_id=event.payload.get("itemId"))
    return TerminalDeliveryIdentity(
        delivery_id=stable_terminal_item_delivery_id(
            thread_id=terminal_key[0],
            turn_id=terminal_key[1],
            item_id=item_id,
        ),
        thread_id=terminal_key[0],
        turn_id=terminal_key[1],
    )


def event_message_identity(
    event,
    message: OutboundMessage | None,
) -> TerminalDeliveryIdentity | None:
    if message is None or event_terminal_identity(event) is None:
        return None
    terminal_key = event_terminal_key(event)
    if terminal_key is None:
        return None
    return message_identity(terminal_key, message)


def message_identity(
    terminal_key: tuple[str, str],
    message: OutboundMessage,
) -> TerminalDeliveryIdentity | None:
    delivery_id = str(message.metadata.get("delivery_id") or "")
    if not delivery_id:
        return None
    return TerminalDeliveryIdentity(
        delivery_id=delivery_id,
        thread_id=terminal_key[0],
        turn_id=terminal_key[1],
    )


def recovered_turn_identities(
    *,
    thread_id: str,
    turn: dict,
    include_turn_fallback: bool = False,
) -> list[TerminalDeliveryIdentity]:
    turn_id = str(turn.get("id") or turn.get("turnId") or "")
    if not thread_id or not turn_id:
        return []
    final_items = recovered_turn_final_items(turn)
    identities = [
        TerminalDeliveryIdentity(
            delivery_id=stable_terminal_item_delivery_id(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=terminal_item_key(item),
            ),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        for item in final_items
    ]
    delivery_ids = [identity.delivery_id for identity in identities]
    if len(set(delivery_ids)) != len(delivery_ids):
        raise ValueError(
            "recovered native answer items have no distinct stable identity"
        )
    if include_turn_fallback or not final_items:
        identities.append(
            TerminalDeliveryIdentity(
                delivery_id=stable_terminal_turn_delivery_id(
                    thread_id=thread_id,
                    turn_id=turn_id,
                ),
                thread_id=thread_id,
                turn_id=turn_id,
            )
        )
    return identities


def recovered_turn_final_items(turn: dict) -> list[dict]:
    items = turn.get("items")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if (
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and item.get("phase") == "final_answer"
            and str(item.get("text") or "").strip()
        )
    ]


def terminal_item_key(item: dict, *, fallback_item_id=None) -> str:
    item_id = str(item.get("id") or fallback_item_id or item.get("itemId") or "")
    if item_id:
        return item_id
    canonical = json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"payload:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _stable_delivery_id(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"imcodex:native:{digest.hexdigest()}"

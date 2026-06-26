from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..appserver import normalize_appserver_message, summarize_transport_message
from ..models import NativeAppServerJournalEntry


DEFAULT_NATIVE_EVENTS_RENDER_LIMIT = 12
MAX_NATIVE_EVENTS_RENDER_LIMIT = 50

_SUMMARY_KEYS = (
    "transport_shape",
    "response_id",
    "has_error",
    "error_code",
    "error_message",
    "result_keys",
    "result_type",
    "payload_keys",
    "payload_key_count",
    "payload_keys_sampled",
    "payload_keys_omitted",
    "item_type",
    "item_status",
    "item_phase",
    "turn_status",
    "turn_payload_id",
    "delta_preview",
    "message_preview",
    "command",
    "tool",
    "server",
    "question_count",
    "questions",
    "changed_paths",
    "permissions_keys",
)


def record_native_appserver_journal(
    store,
    message: dict[str, Any],
    *,
    outcome: str = "received",
    note: str | None = None,
) -> NativeAppServerJournalEntry:
    event = normalize_appserver_message(message)
    summary = summarize_transport_message(message, max_preview_chars=120)
    return store.append_native_appserver_event(
        seen_at=store.clock(),
        direction=event.direction,
        method=event.method,
        category=event.category,
        kind=event.kind,
        thread_id=event.thread_id,
        turn_id=event.turn_id,
        item_id=event.item_id,
        request_id=event.request_id,
        outcome=outcome,
        note=note,
        summary=_compact_summary(summary),
    )


def render_native_events(
    entries: Iterable[NativeAppServerJournalEntry],
    *,
    filters: list[str] | tuple[str, ...] | None = None,
) -> str:
    visible = list(entries)
    filter_label = " ".join(filters or [])
    header = "Native events"
    if filter_label:
        header += f" (filter: {filter_label})"
    if not visible:
        return "\n".join([header, "(none)"])
    lines = [header]
    for entry in visible:
        lines.extend(_render_entry(entry))
    return "\n".join(lines)


def clamp_native_events_limit(limit: object) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_NATIVE_EVENTS_RENDER_LIMIT
    return min(max(value, 1), MAX_NATIVE_EVENTS_RENDER_LIMIT)


def select_native_events(
    store,
    *,
    limit: int,
    filters: list[str] | tuple[str, ...] | None = None,
) -> list[NativeAppServerJournalEntry]:
    entries = store.list_native_appserver_events(limit=MAX_NATIVE_EVENTS_RENDER_LIMIT)
    normalized_filters = [token.strip().lower() for token in (filters or []) if token.strip()]
    if normalized_filters:
        entries = [
            entry
            for entry in entries
            if all(_event_matches_filter(entry, token) for token in normalized_filters)
        ]
    return entries[-limit:]


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in _SUMMARY_KEYS if key in summary}


def _render_entry(entry: NativeAppServerJournalEntry) -> list[str]:
    method = entry.method or "(no method)"
    status = entry.outcome or "received"
    lines = [
        f"#{entry.sequence} {entry.direction} {method} [{entry.category}/{entry.kind}; {status}]"
    ]
    ids = _identity_bits(entry)
    if ids:
        lines.append(f"  {' '.join(ids)}")
    detail = _summary_bits(entry.summary)
    if entry.note:
        detail.append(f"note={_compact_text(entry.note, 80)}")
    if detail:
        lines.append(f"  {'; '.join(detail)}")
    return lines


def _identity_bits(entry: NativeAppServerJournalEntry) -> list[str]:
    bits: list[str] = []
    if entry.request_id:
        bits.append(f"request={_compact_text(entry.request_id, 72)}")
    if entry.thread_id:
        bits.append(f"thread={_compact_text(entry.thread_id, 40)}")
    if entry.turn_id:
        bits.append(f"turn={_compact_text(entry.turn_id, 40)}")
    if entry.item_id:
        bits.append(f"item={_compact_text(entry.item_id, 40)}")
    return bits


def _summary_bits(summary: dict[str, Any]) -> list[str]:
    bits: list[str] = []
    if summary.get("has_error") is True:
        error = summary.get("error_message") or summary.get("error_code") or "yes"
        bits.append(f"error={_compact_text(str(error), 80)}")
    if summary.get("item_type"):
        bits.append(f"item_type={_compact_text(str(summary['item_type']), 40)}")
    if summary.get("item_phase"):
        bits.append(f"phase={_compact_text(str(summary['item_phase']), 40)}")
    if summary.get("item_status"):
        bits.append(f"item_status={_compact_text(str(summary['item_status']), 40)}")
    if summary.get("turn_status"):
        bits.append(f"turn_status={_compact_text(str(summary['turn_status']), 40)}")
    if summary.get("command"):
        bits.append(f"command={_compact_text(str(summary['command']), 100)}")
    if summary.get("tool"):
        bits.append(f"tool={_compact_text(str(summary['tool']), 60)}")
    if summary.get("server"):
        bits.append(f"server={_compact_text(str(summary['server']), 60)}")
    if summary.get("message_preview"):
        bits.append(f"message={_compact_text(str(summary['message_preview']), 100)}")
    if summary.get("delta_preview"):
        bits.append(f"delta={_compact_text(str(summary['delta_preview']), 100)}")
    if summary.get("question_count") is not None:
        bits.append(f"questions={summary['question_count']}")
    if summary.get("changed_paths"):
        bits.append(f"changed_paths={_list_preview(summary['changed_paths'])}")
    if summary.get("permissions_keys"):
        bits.append(f"permissions={_list_preview(summary['permissions_keys'])}")
    if summary.get("payload_keys"):
        bits.append(f"keys={_list_preview(summary['payload_keys'])}")
    elif summary.get("payload_key_count") is not None:
        bits.append(
            "payload_keys="
            f"{summary.get('payload_key_count')} sampled={summary.get('payload_keys_sampled', 0)}"
        )
    if summary.get("result_keys"):
        bits.append(f"result_keys={_list_preview(summary['result_keys'])}")
    elif summary.get("result_type"):
        bits.append(f"result_type={summary['result_type']}")
    return bits


def _list_preview(value: Any, *, max_items: int = 4) -> str:
    if not isinstance(value, list):
        return _compact_text(str(value), 80)
    rendered = [_compact_text(str(item), 40) for item in value[:max_items]]
    if len(value) > max_items:
        rendered.append(f"+{len(value) - max_items}")
    return ",".join(rendered)


def _compact_text(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _event_matches_filter(entry: NativeAppServerJournalEntry, token: str) -> bool:
    if "=" in token:
        field, _, wanted = token.partition("=")
        return _event_field_value(entry, field.strip()).find(wanted.strip().lower()) >= 0
    fields = [
        entry.direction,
        entry.method,
        entry.category,
        entry.kind,
        entry.thread_id,
        entry.turn_id,
        entry.item_id,
        entry.request_id or "",
        entry.outcome or "",
        entry.note or "",
    ]
    if any(token in str(value).lower() for value in fields if value):
        return True
    for key, value in entry.summary.items():
        if token in str(key).lower():
            return True
        if isinstance(value, (str, int, float, bool)) and token in str(value).lower():
            return True
        if isinstance(value, list) and _list_matches_filter(value, token):
            return True
    return False


def _event_field_value(entry: NativeAppServerJournalEntry, field: str) -> str:
    aliases = {
        "dir": "direction",
        "request": "request_id",
        "request_id": "request_id",
        "thread": "thread_id",
        "thread_id": "thread_id",
        "turn": "turn_id",
        "turn_id": "turn_id",
        "item": "item_id",
        "item_id": "item_id",
    }
    name = aliases.get(field, field)
    if name in {
        "direction",
        "method",
        "category",
        "kind",
        "thread_id",
        "turn_id",
        "item_id",
        "request_id",
        "outcome",
        "note",
    }:
        return str(getattr(entry, name) or "").lower()
    value = entry.summary.get(name)
    if value is None:
        return ""
    return str(value).lower()


def _list_matches_filter(values: list[Any], token: str) -> bool:
    for item in values[:5]:
        if isinstance(item, (str, int, float, bool)) and token in str(item).lower():
            return True
        if isinstance(item, dict) and any(
            token in str(nested_key).lower()
            or (
                isinstance(nested_value, (str, int, float, bool))
                and token in str(nested_value).lower()
            )
            for nested_key, nested_value in item.items()
        ):
            return True
    return False

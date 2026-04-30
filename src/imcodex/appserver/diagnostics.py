from __future__ import annotations

import hashlib
from typing import Any

from .protocol_map import normalize_appserver_message


_MAX_UNKNOWN_PAYLOAD_KEYS = 20


def summarize_transport_message(message: dict[str, Any], *, max_preview_chars: int = 240) -> dict[str, Any]:
    transport_shape = _transport_shape(message)
    summary: dict[str, Any] = {"transport_shape": transport_shape}

    if transport_shape == "response":
        summary["response_id"] = message.get("id")
        if "error" in message:
            summary["has_error"] = True
            error = message.get("error")
            if isinstance(error, dict):
                summary["error_code"] = error.get("code")
                summary["error_message"] = _trim_preview(str(error.get("message") or ""), max_preview_chars)
            else:
                summary["error_message"] = _trim_preview(str(error), max_preview_chars)
        else:
            result = message.get("result")
            summary["has_error"] = False
            if isinstance(result, dict):
                summary["result_keys"] = sorted(result.keys())
            else:
                summary["result_type"] = type(result).__name__
        return summary

    if "method" not in message:
        return summary

    event = normalize_appserver_message(message)
    payload = event.payload
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
    summary.update(
        {
            "method": event.method,
            "category": event.category,
            "kind": event.kind,
            "direction": event.direction,
        }
    )
    if event.kind == "unknown":
        summary["payload_key_count"] = len(payload)
        summary["payload_keys_sampled"] = min(len(payload), _MAX_UNKNOWN_PAYLOAD_KEYS)
        summary["payload_keys_omitted"] = max(0, len(payload) - _MAX_UNKNOWN_PAYLOAD_KEYS)
        summary["payload_key_fingerprints"] = [
            {
                "key_sha256": _sha256_text(key),
                "key_length": len(key),
                "value_type": type(value).__name__,
            }
            for key, value in sorted(payload.items())[:_MAX_UNKNOWN_PAYLOAD_KEYS]
        ]
        return summary
    summary.update(
        {
            "thread_id": event.thread_id or None,
            "turn_id": event.turn_id or None,
            "item_id": event.item_id or None,
            "request_id": event.request_id,
        }
    )
    summary["payload_keys"] = sorted(payload.keys())
    if item:
        summary["item_type"] = item.get("type")
        if item.get("status") is not None:
            summary["item_status"] = item.get("status")
        if item.get("phase") is not None:
            summary["item_phase"] = item.get("phase")
    if turn:
        if turn.get("status") is not None:
            summary["turn_status"] = turn.get("status")
        if turn.get("id") is not None:
            summary["turn_payload_id"] = turn.get("id")
    if "delta" in payload:
        summary["delta_preview"] = _trim_preview(str(payload.get("delta") or ""), max_preview_chars)
    if "summaryIndex" in payload:
        summary["summary_index"] = payload.get("summaryIndex")
    if "contentIndex" in payload:
        summary["content_index"] = payload.get("contentIndex")
    if "message" in payload:
        summary["message_preview"] = _trim_preview(str(payload.get("message") or ""), max_preview_chars)
    command = item.get("command") if item else payload.get("command")
    if command:
        summary["command"] = _trim_preview(str(command), max_preview_chars)
    cwd = item.get("cwd") if item else payload.get("cwd")
    if cwd:
        summary["cwd"] = str(cwd)
    for key in ("tool", "server"):
        value = item.get(key) if item else None
        if value is None:
            value = payload.get(key)
        if value is not None:
            summary[key] = value
    if isinstance(payload.get("questions"), list):
        questions = payload.get("questions") or []
        summary["question_count"] = len(questions)
        summary["questions"] = [
            {
                "id": question.get("id"),
                "header": question.get("header"),
                "question": _trim_preview(str(question.get("question") or ""), max_preview_chars),
            }
            for question in questions[:3]
            if isinstance(question, dict)
        ]
    if item and item.get("changes") is not None and isinstance(item.get("changes"), list):
        changes = [change.get("path") for change in item.get("changes", []) if isinstance(change, dict) and change.get("path")]
        summary["changed_paths"] = changes[:10]
    if isinstance(payload.get("permissions"), dict):
        summary["permissions_keys"] = sorted((payload.get("permissions") or {}).keys())
    return summary


def summarize_text(value: str, *, max_preview_chars: int = 240) -> dict[str, Any]:
    return {
        "text_preview": _trim_preview(value, max_preview_chars),
        "text_length": len(value),
        "text_sha256": _sha256_text(value),
    }


def _transport_shape(message: dict[str, Any]) -> str:
    if "id" in message and ("result" in message or "error" in message):
        return "response"
    if "id" in message and "method" in message:
        return "request"
    if "method" in message:
        return "notification"
    return "unknown"


def _trim_preview(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

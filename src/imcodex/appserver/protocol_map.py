from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(slots=True)
class AppServerEvent:
    direction: str
    method: str
    category: str
    kind: str
    payload: dict[str, Any]
    thread_id: str = ""
    turn_id: str = ""
    item_id: str = ""
    request_id: str | None = None
    process_id: str | None = None
    watch_id: str | None = None


_EVENT_KINDS = {
    "item/started": "item_started",
    "item/mcpToolCall/progress": "mcp_tool_progress",
    "item/commandExecution/requestApproval": "approval_request",
    "item/fileChange/requestApproval": "approval_request",
    "item/permissions/requestApproval": "approval_request",
    "item/tool/requestUserInput": "question_request",
    "item/agentMessage/delta": "agent_delta",
    "item/reasoning/summaryTextDelta": "reasoning_summary_text_delta",
    "item/reasoning/summaryPartAdded": "reasoning_summary_part_added",
    "item/reasoning/textDelta": "reasoning_text_delta",
    "turn/started": "turn_started",
    "serverRequest/resolved": "request_resolved",
    "turn/plan/updated": "plan_updated",
    "turn/diff/updated": "diff_updated",
    "thread/name/updated": "thread_name_updated",
    "item/completed": "item_completed",
    "turn/completed": "turn_completed",
    "thread/status/changed": "thread_status_changed",
    "thread/compacted": "thread_compacted",
    "model/rerouted": "model_rerouted",
    "configWarning": "config_warning",
    "deprecationNotice": "deprecation_notice",
}

_SERVER_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/permissions/requestApproval",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "applyPatchApproval",
    "execCommandApproval",
}

_CATEGORY_PREFIXES = (
    ("thread/realtime/", "realtime"),
    ("thread/", "thread"),
    ("turn/", "turn"),
    ("item/", "item"),
    ("rawResponseItem/", "item"),
    ("command/exec/", "command_exec"),
    ("command/", "command_exec"),
    ("fs/", "fs"),
    ("skills/", "skills"),
    ("app/", "app"),
    ("plugin/", "plugin"),
    ("mcpServer/", "mcp"),
    ("mcpTool", "mcp"),
    ("account/", "account"),
    ("config/", "config"),
    ("windowsSandbox/", "system"),
    ("windows/", "system"),
    ("model/", "system"),
    ("hook/", "system"),
    ("fuzzyFileSearch/", "system"),
)

_SYSTEM_METHODS = {"configWarning", "deprecationNotice", "error"}


def normalize_appserver_message(message: dict[str, Any]) -> AppServerEvent:
    method = str(message.get("method", ""))
    payload = message.get("params", {})
    if not isinstance(payload, dict):
        payload = {}
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    request_id = payload.get("requestId") or payload.get("_request_id")
    item_id = payload.get("itemId") or item.get("id")
    direction = "server_request" if method in _SERVER_REQUEST_METHODS and "id" in message else "notification"
    return AppServerEvent(
        direction=direction,
        method=method,
        category=_categorize_method(method),
        kind=_EVENT_KINDS.get(method, "unknown"),
        payload=payload,
        thread_id=str(payload.get("threadId", "") or ""),
        turn_id=str(payload.get("turnId", "") or ""),
        item_id=str(item_id or ""),
        request_id=str(request_id) if request_id is not None else None,
        process_id=_string_or_none(payload.get("processId")),
        watch_id=_string_or_none(payload.get("watchId")),
    )


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
            "thread_id": event.thread_id or None,
            "turn_id": event.turn_id or None,
            "item_id": event.item_id or None,
            "request_id": event.request_id,
            "payload_keys": sorted(payload.keys()),
        }
    )
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
    if item and item.get("tool") is not None:
        summary["tool"] = item.get("tool")
    elif payload.get("tool") is not None:
        summary["tool"] = payload.get("tool")
    if item and item.get("server") is not None:
        summary["server"] = item.get("server")
    elif payload.get("server") is not None:
        summary["server"] = payload.get("server")
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
    if event.kind == "unknown":
        summary["payload_preview"] = _trim_preview(_safe_json(payload), max_preview_chars)
    return summary


def _categorize_method(method: str) -> str:
    if method in _SYSTEM_METHODS:
        return "system"
    for prefix, category in _CATEGORY_PREFIXES:
        if method.startswith(prefix):
            return category
    return "unknown"


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


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


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except Exception:
        return repr(value)

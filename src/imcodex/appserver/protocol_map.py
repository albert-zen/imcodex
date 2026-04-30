from __future__ import annotations

from dataclasses import dataclass
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

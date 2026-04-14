from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AppServerEvent:
    method: str
    kind: str
    payload: dict[str, Any]
    thread_id: str = ""
    turn_id: str = ""
    request_id: str | None = None


_EVENT_KINDS = {
    "item/commandExecution/requestApproval": "approval_request",
    "item/fileChange/requestApproval": "approval_request",
    "item/tool/requestUserInput": "question_request",
    "item/agentMessage/delta": "agent_delta",
    "turn/started": "turn_started",
    "serverRequest/resolved": "request_resolved",
    "turn/plan/updated": "plan_updated",
    "turn/diff/updated": "diff_updated",
    "thread/name/updated": "thread_name_updated",
    "item/completed": "item_completed",
    "turn/completed": "turn_completed",
}


def normalize_appserver_message(message: dict[str, Any]) -> AppServerEvent:
    method = str(message.get("method", ""))
    payload = message.get("params", {})
    if not isinstance(payload, dict):
        payload = {}
    request_id = payload.get("requestId") or payload.get("_request_id")
    return AppServerEvent(
        method=method,
        kind=_EVENT_KINDS.get(method, "unknown"),
        payload=payload,
        thread_id=str(payload.get("threadId", "") or ""),
        turn_id=str(payload.get("turnId", "") or ""),
        request_id=str(request_id) if request_id is not None else None,
    )

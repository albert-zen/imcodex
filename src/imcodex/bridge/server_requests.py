from __future__ import annotations

from typing import Any

from ..appserver import normalize_appserver_message
from ..models import OutboundMessage
from ..observability.runtime import emit_event


_SYSTEM_PREFIX = "[System] "
_UNSUPPORTED_REQUEST_CODE = -32601


class NativeRequestPolicy:
    def __init__(self, *, store, backend) -> None:
        self.store = store
        self.backend = backend

    def resolution_payload(self, request_id: str, decision: str) -> dict[str, Any]:
        route = self.store.get_pending_request(request_id)
        if route is None:
            return {"decision": decision}
        if route.request_method == "item/permissions/requestApproval":
            permissions = route.payload.get("permissions")
            granted = permissions if decision == "accept" and isinstance(permissions, dict) else {}
            return {"permissions": granted}
        return {"decision": decision}

    async def reject_unrouted(self, request: dict) -> OutboundMessage | None:
        event = normalize_appserver_message(request)
        if event.direction != "server_request":
            return None
        if event.request_id and self.store.get_pending_request(event.request_id) is not None:
            return None
        transport_request_id = self._transport_request_id(request)
        if transport_request_id is not None:
            await self.backend.reply_error_to_transport_request(
                transport_request_id,
                code=_UNSUPPORTED_REQUEST_CODE,
                message=f"unsupported or unroutable server request: {event.method}",
                data={
                    "reason": "unsupportedServerRequest",
                    "method": event.method,
                    "requestId": event.request_id,
                },
            )
        emit_event(
            component="bridge",
            event="bridge.server_request.rejected",
            level="WARNING",
            message="Rejected unsupported or unroutable server request",
            data={
                "method": event.method,
                "request_id": event.request_id,
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
            },
        )
        if not event.thread_id:
            return None
        binding = self.store.find_binding_by_thread_id(event.thread_id)
        if binding is None:
            return None
        return OutboundMessage(
            channel_id=binding.channel_id,
            conversation_id=binding.conversation_id,
            message_type="status",
            text=(
                f"{_SYSTEM_PREFIX}Codex sent an unsupported or unroutable request "
                f"(`{event.method}`), so I rejected it to avoid leaving the turn stuck."
            ),
        )

    def _transport_request_id(self, request: dict) -> str | int | None:
        params = request.get("params")
        if isinstance(params, dict):
            transport_request_id = params.get("_transport_request_id")
            if transport_request_id is not None:
                return transport_request_id
        return request.get("id")

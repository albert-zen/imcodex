from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..appserver import normalize_appserver_message
from ..appserver.protocol_map import HOST_DELEGATED_SERVER_REQUEST_METHODS
from ..models import OutboundMessage
from ..observability.runtime import emit_event


_SYSTEM_PREFIX = "[System] "
_UNSUPPORTED_REQUEST_CODE = -32601
_PEER_HOST_REQUEST_TIMEOUT_S = 60.0
_RECENT_HOST_COMPLETION_LIMIT = 512


@dataclass(slots=True)
class _DelegatedHostRequest:
    task: asyncio.Task[None]
    thread_id: str
    turn_id: str
    call_id: str
    connection_epoch: int | None
    journal_sequence: int | None


class NativeRequestPolicy:
    def __init__(self, *, store, backend, peer_host_request_timeout_s: float = _PEER_HOST_REQUEST_TIMEOUT_S) -> None:
        self.store = store
        self.backend = backend
        self.peer_host_request_timeout_s = max(0.01, float(peer_host_request_timeout_s))
        self._delegated_host_requests: dict[tuple[int | None, str], _DelegatedHostRequest] = {}
        self._recent_dynamic_tool_completions: dict[tuple[str, str, str], None] = {}
        self._recent_turn_completions: dict[tuple[str, str], None] = {}

    def resolution_payload(self, request_id: str, decision: str) -> dict[str, Any]:
        route = self.store.get_pending_request(request_id)
        if route is None:
            return {"decision": decision}
        if route.request_method == "item/permissions/requestApproval":
            permissions = route.payload.get("permissions")
            granted = permissions if decision == "accept" and isinstance(permissions, dict) else {}
            return {"permissions": granted}
        return {"decision": decision}

    def delegate_to_peer_host(self, request: dict, *, journal_sequence: int | None = None) -> bool:
        event = normalize_appserver_message(request)
        if event.method not in HOST_DELEGATED_SERVER_REQUEST_METHODS:
            return False
        facts = self.backend.app_server_connection_facts()
        if facts.get("ownership") != "external":
            return False
        transport_request_id = self._transport_request_id(request)
        if transport_request_id is None:
            return False
        connection_epoch = self._connection_epoch(request)
        key = (connection_epoch, str(transport_request_id))
        call_id = str(event.payload.get("callId") or event.item_id or "")
        already_completed = (
            (event.thread_id, event.turn_id, call_id) in self._recent_dynamic_tool_completions
            or (event.thread_id, event.turn_id) in self._recent_turn_completions
        )
        if already_completed:
            task = asyncio.create_task(asyncio.sleep(0))
        else:
            task = asyncio.create_task(
                self._reject_unclaimed_host_request(
                    request,
                    transport_request_id=transport_request_id,
                    connection_epoch=connection_epoch,
                    journal_sequence=journal_sequence,
                )
            )
        delegated = _DelegatedHostRequest(
            task=task,
            thread_id=event.thread_id,
            turn_id=event.turn_id,
            call_id=call_id,
            connection_epoch=connection_epoch,
            journal_sequence=journal_sequence,
        )
        previous = self._delegated_host_requests.get(key)
        if previous is not None:
            previous.task.cancel()
        self._delegated_host_requests[key] = delegated
        task.add_done_callback(lambda completed, request_key=key: self._forget_delegated(request_key, completed))
        emit_event(
            component="bridge",
            event="bridge.server_request.delegated",
            message="Left host-owned native request for another App Server subscriber",
            data={
                "method": event.method,
                "request_id": event.request_id,
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
            },
        )
        return True

    def observe_notification(self, notification: dict) -> None:
        event = normalize_appserver_message(notification)
        if event.kind == "item_completed":
            item = event.payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "dynamicToolCall":
                return
            call_id = str(item.get("id") or event.item_id or "")
            self._remember_recent_completion(
                self._recent_dynamic_tool_completions,
                (event.thread_id, event.turn_id, call_id),
            )
            self._cancel_matching_delegated(
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                call_id=call_id,
            )
        elif event.kind == "turn_completed":
            self._remember_recent_completion(
                self._recent_turn_completions,
                (event.thread_id, event.turn_id),
            )
            self._cancel_matching_delegated(
                thread_id=event.thread_id,
                turn_id=event.turn_id,
            )

    def cancel_connection_epoch(self, connection_epoch: int) -> None:
        for key, delegated in list(self._delegated_host_requests.items()):
            if delegated.connection_epoch == connection_epoch:
                self._delegated_host_requests.pop(key, None)
                delegated.task.cancel()

    async def close(self) -> None:
        tasks = [delegated.task for delegated in self._delegated_host_requests.values()]
        self._delegated_host_requests.clear()
        self._recent_dynamic_tool_completions.clear()
        self._recent_turn_completions.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reject_unclaimed_host_request(
        self,
        request: dict,
        *,
        transport_request_id: str | int,
        connection_epoch: int | None,
        journal_sequence: int | None,
    ) -> None:
        await asyncio.sleep(self.peer_host_request_timeout_s)
        event = normalize_appserver_message(request)
        await self.backend.reply_error_to_transport_request(
            transport_request_id,
            code=_UNSUPPORTED_REQUEST_CODE,
            message=f"no peer host handled dynamic tool request: {event.payload.get('tool') or '(unknown)'}",
            data={
                "reason": "dynamicToolHostUnavailable",
                "method": event.method,
                "requestId": event.request_id,
            },
            connection_epoch=connection_epoch,
        )
        if journal_sequence is not None:
            self.store.update_native_appserver_event(
                journal_sequence,
                outcome="rejected",
                note="peer host did not resolve delegated dynamic tool request before timeout",
            )
        emit_event(
            component="bridge",
            event="bridge.server_request.delegation_timeout",
            level="WARNING",
            message="Peer host did not resolve delegated dynamic tool request before timeout",
            data={
                "method": event.method,
                "request_id": event.request_id,
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
                "tool": event.payload.get("tool"),
            },
        )

    def _cancel_matching_delegated(
        self,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None = None,
    ) -> None:
        for key, delegated in list(self._delegated_host_requests.items()):
            if delegated.thread_id != thread_id or delegated.turn_id != turn_id:
                continue
            if call_id is not None and delegated.call_id != call_id:
                continue
            self._delegated_host_requests.pop(key, None)
            delegated.task.cancel()

    def _forget_delegated(self, key: tuple[int | None, str], task: asyncio.Task[None]) -> None:
        delegated = self._delegated_host_requests.get(key)
        if delegated is not None and delegated.task is task:
            self._delegated_host_requests.pop(key, None)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                emit_event(
                    component="bridge",
                    event="bridge.server_request.delegation_failed",
                    level="WARNING",
                    message=str(exception) or "Delegated host request fallback failed",
                    data={"error_type": type(exception).__name__},
                )

    @staticmethod
    def _remember_recent_completion(cache: dict[tuple, None], key: tuple) -> None:
        cache.pop(key, None)
        cache[key] = None
        while len(cache) > _RECENT_HOST_COMPLETION_LIMIT:
            cache.pop(next(iter(cache)))

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
                connection_epoch=self._connection_epoch(request),
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

    def _connection_epoch(self, request: dict) -> int | None:
        params = request.get("params")
        if not isinstance(params, dict):
            return None
        try:
            epoch = int(params.get("_connection_epoch") or 0)
        except (TypeError, ValueError):
            return None
        return epoch or None

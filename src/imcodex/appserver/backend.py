from __future__ import annotations

import copy

from ..store import ConversationStore
from .backend_errors import CodexBackendErrorMixin
from .backend_types import (
    ACTIVE_THREAD_STATUSES as ACTIVE_THREAD_STATUSES,
    StaleThreadBindingError as StaleThreadBindingError,
    ThreadListResult as ThreadListResult,
    ThreadSelectionError as ThreadSelectionError,
    TurnSubmission as TurnSubmission,
)
from .client import AppServerError
from .settings_backend import (
    PERMISSION_MODE_PROFILE_IDS as PERMISSION_MODE_PROFILE_IDS,
    CodexSettingsBackendMixin,
)
from .thread_backend import CodexThreadBackendMixin


class CodexBackend(CodexThreadBackendMixin, CodexSettingsBackendMixin, CodexBackendErrorMixin):
    def __init__(
        self,
        *,
        client,
        store: ConversationStore,
        service_name: str,
        thread_dynamic_tools: list[dict] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name
        self.thread_dynamic_tools = copy.deepcopy(thread_dynamic_tools)

    def prefers_native_recovery(self) -> bool:
        preserves_server_state = getattr(self.client, "preserves_server_state", None)
        if preserves_server_state is not None:
            return bool(preserves_server_state)
        mode = getattr(self.client, "connection_mode", "") or getattr(self.client, "last_connection_mode", "")
        if mode == "disconnected":
            mode = getattr(self.client, "last_connection_mode", "")
        return mode in {"external", "dedicated-ws", "shared-ws"}

    def app_server_connection_facts(self) -> dict:
        provider = getattr(self.client, "connection_facts", None)
        if callable(provider):
            return dict(provider())
        mode = str(getattr(self.client, "connection_mode", "") or "disconnected")
        connected = mode != "disconnected"
        if mode in {"external", "dedicated-ws", "shared-ws"}:
            ownership = "external"
        elif mode == "spawned-stdio":
            ownership = "bridge-child"
        else:
            ownership = "unknown"
        return {
            "connected": connected,
            "ready": connected and bool(getattr(self.client, "initialized", False)),
            "status": "connected" if connected else "disconnected",
            "mode": mode,
            "ownership": ownership,
            "transport": "unknown",
            "endpoint": "(unknown)",
            "connection_epoch": int(getattr(self.client, "connection_epoch", 0) or 0),
            "reconnect_enabled": self.prefers_native_recovery(),
        }

    async def reply_to_server_request(self, request_id: str, decision_or_answers: dict) -> None:
        route = self.store.get_pending_request(request_id)
        if route is None or route.transport_request_id is None:
            raise AppServerError(f"unknown pending request: {request_id}")
        await self.reply_to_transport_request(
            route.transport_request_id,
            decision_or_answers,
            connection_epoch=route.connection_epoch,
        )
        self.store.remove_pending_request(request_id)

    async def reply_to_transport_request(
        self,
        transport_request_id: str | int,
        result: dict,
        *,
        connection_epoch: int | None = None,
    ) -> None:
        await self.client.reply_to_transport_request(
            transport_request_id,
            result,
            expected_connection_epoch=connection_epoch,
        )

    async def reply_error_to_server_request(
        self,
        request_id: str,
        *,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        route = self.store.get_pending_request(request_id)
        if route is None or route.transport_request_id is None:
            raise AppServerError(f"unknown pending request: {request_id}")
        await self.client.reply_error_to_transport_request(
            route.transport_request_id,
            code=code,
            message=message,
            data=data,
            expected_connection_epoch=route.connection_epoch,
        )
        self.store.remove_pending_request(request_id)

    async def reply_error_to_transport_request(
        self,
        transport_request_id: str | int,
        *,
        code: int,
        message: str,
        data: object | None = None,
        connection_epoch: int | None = None,
    ) -> None:
        await self.client.reply_error_to_transport_request(
            transport_request_id,
            code=code,
            message=message,
            data=data,
            expected_connection_epoch=connection_epoch,
        )

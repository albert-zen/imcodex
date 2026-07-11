from __future__ import annotations

from ..store import ConversationStore
from .backend_errors import CodexBackendErrorMixin
from .backend_types import (
    ACTIVE_THREAD_STATUSES,
    StaleThreadBindingError,
    ThreadListResult,
    ThreadSelectionError,
    TurnSubmission,
)
from .client import AppServerError
from .settings_backend import PERMISSION_MODE_PROFILE_IDS, CodexSettingsBackendMixin
from .thread_backend import CodexThreadBackendMixin


class CodexBackend(CodexThreadBackendMixin, CodexSettingsBackendMixin, CodexBackendErrorMixin):
    def __init__(self, *, client, store: ConversationStore, service_name: str) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name

    def prefers_native_recovery(self) -> bool:
        preserves_server_state = getattr(self.client, "preserves_server_state", None)
        if preserves_server_state is not None:
            return bool(preserves_server_state)
        mode = getattr(self.client, "connection_mode", "") or getattr(self.client, "last_connection_mode", "")
        if mode == "disconnected":
            mode = getattr(self.client, "last_connection_mode", "")
        return mode in {"external", "dedicated-ws", "shared-ws"}

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

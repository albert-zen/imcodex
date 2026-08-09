from __future__ import annotations

import copy
from collections.abc import Callable

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
        thread_observer=None,
    ) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name
        self.thread_dynamic_tools = copy.deepcopy(thread_dynamic_tools)
        self.thread_observer = thread_observer
        # A native thread has no resumable rollout until its first turn starts.
        # Keep that transient fact process-local so the first input can use the
        # live connection instead of asking Codex to resume nonexistent history.
        self._unpersisted_thread_ids: set[str] = set()
        self._binding_reconciled_handlers: list[Callable[[str, str, str], None]] = []
        self._active_rehydration_tokens: dict[tuple[str, str, str], object] = {}

    def add_binding_reconciled_handler(
        self,
        handler: Callable[[str, str, str], None],
    ) -> None:
        self._binding_reconciled_handlers.append(handler)

    def _notify_binding_reconciled(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> None:
        key = (channel_id, conversation_id, thread_id)
        # Ordinary exact resume/attach supersedes any background reconciliation
        # already awaiting native state for this binding.
        self._active_rehydration_tokens.pop(key, None)
        for handler in tuple(self._binding_reconciled_handlers):
            handler(channel_id, conversation_id, thread_id)

    async def close(self) -> None:
        close = getattr(self.thread_observer, "close", None)
        if callable(close):
            await close()

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

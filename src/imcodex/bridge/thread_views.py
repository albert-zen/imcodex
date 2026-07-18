from __future__ import annotations

import asyncio

from ..appserver import AppServerError
from ..models import InboundMessage, NativeThreadSnapshot
from .settings import (
    current_model_label,
    current_reasoning_label,
    effective_config,
    fast_mode_label,
    permission_mode_label,
)


_THREADS_PAGE_SIZE = 5
_STATUS_QUERY_TIMEOUT_S = 2.5


class ThreadViewMixin:
    async def _render_threads(
        self,
        message: InboundMessage,
        *,
        page: int = 1,
        query: str | None = None,
    ) -> str:
        requested_page = max(page, 1)
        threads, safe_page, page_count, next_cursor, page_cursors = await self._load_thread_page(
            message,
            requested_page=requested_page,
            query=query,
        )
        visible = threads[:_THREADS_PAGE_SIZE]
        if not visible:
            self.store.set_thread_browser_context(
                message.channel_id,
                message.conversation_id,
                thread_ids=[],
                page=1,
                total=1,
                query=query,
                next_cursor=None,
                page_cursors=[None],
            )
            return "\n".join(
                [
                    "Threads (Page 1/1)",
                    "(none)",
                    "Use /threads <keyword> to filter, or /new to start a fresh thread.",
                ]
            )
        self.store.set_thread_browser_context(
            message.channel_id,
            message.conversation_id,
            thread_ids=[snapshot.thread_id for snapshot in visible],
            page=safe_page,
            total=page_count,
            query=query,
            next_cursor=next_cursor,
            page_cursors=page_cursors,
        )
        lines = [self._thread_page_heading(safe_page, page_count, has_more=next_cursor is not None)]
        for index, snapshot in enumerate(visible, start=1):
            details = [snapshot.status]
            if snapshot.thread_id == self.store.get_binding(message.channel_id, message.conversation_id).thread_id:
                details.append("current")
            lines.append(
                f"{index}. {self._thread_label(snapshot)} · "
                f"【Workspace: {self._thread_workspace_label(snapshot)}】 · "
                f"({', '.join(details)})"
            )
        actions = ["Use /pick <n> to switch", "/new to start fresh", "/exit to close"]
        if safe_page < page_count:
            actions.insert(1, "/next for more")
        if safe_page > 1:
            actions.insert(1, "/prev for previous")
        if query is None:
            actions.append("/threads <keyword> to filter")
        return "\n".join(lines + ["; ".join(actions) + "."])

    async def _load_thread_page(
        self,
        message: InboundMessage,
        *,
        requested_page: int,
        query: str | None,
    ) -> tuple[list[NativeThreadSnapshot], int, int, str | None, list[str | None]]:
        context = self.store.get_thread_browser_context(message.channel_id, message.conversation_id)
        page_cursors = (
            list(context.page_cursors)
            if context is not None and context.query == query and context.page_cursors
            else [None]
        )
        current_page = requested_page if requested_page <= len(page_cursors) else len(page_cursors)
        current_page = max(1, current_page)
        while True:
            cursor = page_cursors[current_page - 1] if current_page - 1 < len(page_cursors) else None
            query_result = await self.backend.query_threads(
                message.channel_id,
                message.conversation_id,
                search_term=query,
                limit=_THREADS_PAGE_SIZE,
                cursor=cursor,
            )
            page_cursors = self._remember_thread_page_cursor(
                page_cursors,
                page=current_page,
                next_cursor=query_result.next_cursor,
            )
            if current_page == requested_page or not query_result.next_cursor:
                page_count = current_page + (1 if query_result.next_cursor else 0)
                return (
                    query_result.threads,
                    current_page,
                    page_count,
                    query_result.next_cursor,
                    page_cursors,
                )
            current_page += 1

    def _remember_thread_page_cursor(
        self,
        page_cursors: list[str | None],
        *,
        page: int,
        next_cursor: str | None,
    ) -> list[str | None]:
        page_cursors = list(page_cursors[:page])
        if next_cursor:
            page_cursors.append(next_cursor)
        return page_cursors or [None]

    def _thread_page_heading(self, page: int, page_count: int, *, has_more: bool) -> str:
        page_count_label = f"{page_count}+" if has_more else str(page_count)
        return f"Threads (Page {page}/{page_count_label})"

    async def _render_status(self, message: InboundMessage) -> str:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        cwd = self.store.current_cwd(message.channel_id, message.conversation_id) or "(none)"
        config = await self._read_status_config(message.channel_id, message.conversation_id)
        current_config = effective_config(config)
        if binding.thread_id is None:
            thread_label = "(none)"
            state = "Idle"
        else:
            try:
                snapshot = await asyncio.wait_for(
                    self.backend.read_thread(
                        message.channel_id,
                        message.conversation_id,
                        binding.thread_id,
                    ),
                    timeout=_STATUS_QUERY_TIMEOUT_S,
                )
            except (asyncio.TimeoutError, AppServerError):
                snapshot = None
            if snapshot is None:
                thread_label = binding.thread_id
                state = "Unavailable"
            else:
                thread_label = self._thread_label(snapshot)
                cwd = snapshot.cwd or cwd
                active = self.store.get_active_turn(binding.thread_id)
                state = "Working" if active and active[1] == "inProgress" else self._human_state(snapshot.status)
        app_server = self.backend.app_server_connection_facts()
        fast_label = fast_mode_label(
            current_config,
            default_service_tier=config.get("selectedModelDefaultServiceTier"),
            feature_available=config.get("fastAvailable") is not False,
            fast_supported=(
                config.get("fastSupported") if isinstance(config.get("fastSupported"), bool) else None
            ),
        )
        return "\n".join(
            [
                "Status",
                "",
                f"CWD: {cwd}",
                f"Thread: {thread_label}",
                f"State: {state}",
                f"App Server: {self._app_server_status_label(app_server)}",
                f"Ownership: {self._app_server_ownership_label(app_server)}",
                f"Transport: {self._app_server_transport_label(app_server)}",
                f"Endpoint: {app_server.get('endpoint') or '(unknown)'}",
                f"Connection epoch: {int(app_server.get('connection_epoch') or 0)}",
                f"Model: {current_model_label(current_config)}",
                f"Reasoning: {current_reasoning_label(current_config)}",
                f"Fast mode: {fast_label}",
                f"Permissions: {permission_mode_label(current_config)}",
                f"Bridge visibility: {self._bridge_visibility_label(binding)}",
                f"Pending approvals: {len(self.store.list_pending_requests(message.channel_id, message.conversation_id, kind='approval'))}",
            ]
        )

    async def _render_thread(self, message: InboundMessage, thread_id: str | None) -> str:
        if thread_id is None:
            return "No active thread."
        try:
            snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, thread_id)
        except AppServerError as exc:
            return f"Current thread {thread_id} could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
        if snapshot is None:
            return f"Current thread {thread_id} is no longer available in Codex."
        return "\n".join(
            [
                f"Thread: {self._thread_label(snapshot)}",
                f"Thread id: {snapshot.thread_id}",
                f"Workspace: {self._thread_workspace_label(snapshot)}",
                f"CWD: {snapshot.cwd or '(unknown)'}",
                f"Path: {snapshot.path or snapshot.cwd or '(unknown)'}",
                f"Status: {snapshot.status}",
                f"Source: {snapshot.source or 'unknown'}",
            ]
        )

    def _thread_label(self, snapshot) -> str:
        for candidate in (snapshot.name, snapshot.preview, snapshot.path, snapshot.cwd):
            if not candidate:
                continue
            text = str(candidate).strip()
            if text:
                if "\\" in text or "/" in text:
                    text = text.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                return text
        return snapshot.thread_id

    def _thread_workspace_label(self, snapshot) -> str:
        for candidate in (snapshot.cwd, snapshot.path):
            if not candidate:
                continue
            text = str(candidate).strip()
            if not text:
                continue
            if "\\" in text or "/" in text:
                text = text.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            return text or "(unknown)"
        return "(unknown)"

    def _bridge_visibility_label(self, binding) -> str:
        return binding.visibility_profile.replace("-", " ").title()

    def _app_server_status_label(self, facts: dict) -> str:
        labels = {
            "connected": "Connected",
            "initializing": "Initializing",
            "reconnecting": "Reconnecting",
            "disconnected": "Disconnected",
        }
        status = str(facts.get("status") or "disconnected")
        return labels.get(status, status.replace("-", " ").title())

    def _app_server_ownership_label(self, facts: dict) -> str:
        if facts.get("ownership") == "external":
            return "Externally managed"
        if facts.get("ownership") == "bridge-child":
            return "Bridge child (compatibility)"
        return "Unknown"

    def _app_server_transport_label(self, facts: dict) -> str:
        labels = {
            "unix-websocket": "Unix WebSocket",
            "tcp-websocket": "TCP WebSocket",
            "stdio-jsonl": "stdio JSONL",
        }
        transport = str(facts.get("transport") or "unknown")
        return labels.get(transport, transport.replace("-", " ").title())

    def _human_state(self, status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"inprogress", "in_progress", "working", "running"}:
            return "Working"
        if normalized == "completed":
            return "Completed"
        if normalized == "failed":
            return "Failed"
        return "Idle" if normalized == "idle" else str(status or "Idle").title()

    async def _read_status_config(self, channel_id: str, conversation_id: str) -> dict:
        try:
            base = await asyncio.wait_for(
                self.backend.read_effective_settings(
                    channel_id,
                    conversation_id,
                    include_model_metadata=False,
                ),
                timeout=_STATUS_QUERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, AppServerError):
            return {"config": {}}
        try:
            return await asyncio.wait_for(
                self.backend.enrich_effective_settings(base),
                timeout=_STATUS_QUERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, AppServerError):
            return base

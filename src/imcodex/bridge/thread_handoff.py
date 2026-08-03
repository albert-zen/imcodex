from __future__ import annotations

from imagent.applications.appserver_client import AppServerError

from ..appserver import ThreadSelectionError
from ..models import InboundMessage, OutboundMessage
from .thread_history import render_thread_catchup, render_thread_history

_ACTIVE_THREAD_STATUSES = frozenset(
    {"active", "inprogress", "in_progress", "running", "working"}
)


class ThreadHandoffMixin:
    """Product Thread navigation over authoritative SDK-backed native reads."""

    async def _switch_thread(
        self,
        message: InboundMessage,
        thread_id: str,
        *,
        history_limit: object,
        catchup_limit: object,
        verb: str,
    ) -> list[OutboundMessage]:
        try:
            attached_id = await self.backend.attach_thread(
                message.channel_id,
                message.conversation_id,
                thread_id,
            )
        except ThreadSelectionError as exc:
            return [self._message(message, "error", str(exc))]
        except AppServerError as exc:
            return [
                self._message(
                    message,
                    "status",
                    f"Thread could not be attached: {self._safe_appserver_error(exc)}.",
                )
            ]

        snapshot = self.store.get_thread_snapshot(attached_id)
        label = self._thread_label(snapshot) if snapshot is not None else attached_id
        status = snapshot.status if snapshot is not None else "idle"
        running = self._thread_status_is_active(status)
        lines = [f"{verb} {label}.", f"State: {'Working' if running else 'Idle'}"]
        if snapshot is not None and snapshot.cwd:
            lines.append(f"CWD: {snapshot.cwd}")

        requested_history = self._positive_int(history_limit)
        requested_catchup = self._positive_int(catchup_limit)
        if running and requested_history is not None:
            lines.append(
                "History was not shown because this thread is currently running; "
                f"ignored --history {requested_history}."
            )
        if running:
            lines.append("Now following native updates for this thread here.")
        outbound = [self._message(message, "status", "\n".join(lines))]
        if requested_catchup is not None:
            outbound.extend(
                await self._read_thread_catchup(message, limit=requested_catchup)
            )
        elif requested_history is not None and not running:
            outbound.extend(
                await self._read_thread_history(
                    message,
                    limit=requested_history,
                    page=1,
                )
            )
        return outbound

    async def _handle_direct_thread_pick(
        self,
        message: InboundMessage,
        *,
        query: str,
        history_limit: object,
        catchup_limit: object,
    ) -> list[OutboundMessage]:
        try:
            result = await self.backend.query_all_threads(
                message.channel_id,
                message.conversation_id,
                search_term=query,
            )
            if len(result.threads) == 1:
                return await self._switch_thread(
                    message,
                    result.threads[0].thread_id,
                    history_limit=history_limit,
                    catchup_limit=catchup_limit,
                    verb="Switched to",
                )
            text = await self._render_threads(
                message,
                page=1,
                query=query if result.threads else None,
                refresh=not result.threads,
                catalog=result.threads if result.threads else None,
            )
        except AppServerError:
            text = (
                "Threads could not be refreshed from Codex right now. "
                "Use /status, /thread read, or try /pick again in a moment."
            )
            return [self._message(message, "status", text)]
        return [self._message(message, "command_result", text)]

    async def _handle_thread_catchup_command(
        self,
        message: InboundMessage,
        *,
        limit: int,
    ) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.thread_id is None:
            return [self._message(message, "command_result", "No active thread.")]
        return await self._read_thread_catchup(message, limit=limit)

    async def _read_thread_catchup(
        self,
        message: InboundMessage,
        *,
        limit: int,
    ) -> list[OutboundMessage]:
        try:
            payload = await self.backend.read_thread_history(
                message.channel_id,
                message.conversation_id,
                limit=1,
                page=1,
            )
        except AppServerError as exc:
            text = (
                "Recent activity could not be queried from Codex right now: "
                f"{self._safe_appserver_error(exc)}."
            )
            return [self._message(message, "command_result", text)]
        return [
            self._message(
                message,
                "command_result",
                render_thread_catchup(payload, limit=limit),
            )
        ]

    async def _handle_thread_history_command(
        self,
        message: InboundMessage,
        *,
        limit: int,
        page: int = 1,
    ) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.thread_id is None:
            return [self._message(message, "command_result", "No active thread.")]
        try:
            snapshot = await self.backend.read_thread(
                message.channel_id,
                message.conversation_id,
                binding.thread_id,
            )
        except AppServerError as exc:
            text = (
                "Thread history could not be queried from Codex right now: "
                f"{self._safe_appserver_error(exc)}."
            )
            return [self._message(message, "command_result", text)]
        if snapshot is None:
            return [
                self._message(
                    message,
                    "command_result",
                    "Thread history is not available.",
                )
            ]
        return await self._read_thread_history(message, limit=limit, page=page)

    async def _read_thread_history(
        self,
        message: InboundMessage,
        *,
        limit: int,
        page: int,
    ) -> list[OutboundMessage]:
        try:
            payload = await self.backend.read_thread_history(
                message.channel_id,
                message.conversation_id,
                limit=limit,
                page=page,
            )
        except AppServerError as exc:
            text = (
                "Thread history could not be queried from Codex right now: "
                f"{self._safe_appserver_error(exc)}."
            )
            return [self._message(message, "command_result", text)]
        return [
            self._message(
                message,
                "command_result",
                render_thread_history(payload, limit=limit),
            )
        ]

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(value) if value is not None else 0
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _thread_status_is_active(status: str) -> bool:
        return str(status or "").replace("-", "_").casefold() in _ACTIVE_THREAD_STATUSES

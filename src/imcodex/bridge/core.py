from __future__ import annotations

from ..appserver import AppServerError, StaleThreadBindingError
from ..models import InboundMessage, OutboundMessage


_SYSTEM_MESSAGE_TYPES = frozenset({"accepted", "status", "command_result", "error"})
_SYSTEM_PREFIX = "[System] "


class BridgeService:
    def __init__(
        self,
        *,
        store,
        backend,
        command_router,
        projector,
        outbound_sink=None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router
        self.projector = projector
        self.outbound_sink = outbound_sink

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        if message.text.startswith("/"):
            return await self._handle_command(message)
        return await self._handle_text(message)

    async def _handle_text(self, message: InboundMessage) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None:
            return [self._message(message, "error", "Choose a CWD first with /cwd <path>.")]
        try:
            submission = await self.backend.submit_text(message.channel_id, message.conversation_id, message.text)
        except StaleThreadBindingError as exc:
            self.store.clear_thread_binding(message.channel_id, message.conversation_id)
            text = (
                f"Current thread {exc.thread_id} could not be resumed. "
                "Use /new or /thread attach <thread-id>."
            )
            return [self._message(message, "status", text)]
        if submission.kind == "start":
            self.store.note_active_turn(submission.thread_id, submission.turn_id, "inProgress")
        return [self._message(message, "accepted", "Accepted. Processing started.")]

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        response = self.command_router.handle(message.channel_id, message.conversation_id, message.text)
        if response.action == "threads.query":
            try:
                text = await self._render_threads(message, response.include_all)
            except AppServerError:
                text = (
                    "Threads could not be refreshed from Codex right now. "
                    "Use /status, /thread read, or try /threads again in a moment."
                )
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", text)]
        if response.action == "status.query":
            try:
                text = await self._render_status(message)
            except AppServerError as exc:
                binding = self.store.get_binding(message.channel_id, message.conversation_id)
                text = (
                    f"Current thread {binding.thread_id} could not be queried from Codex right now: {exc}. "
                    "Try again in a moment."
                )
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", text)]
        if response.action == "thread.read.query":
            return [self._message(message, "command_result", await self._render_thread(message, response.thread_id))]
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            return [self._message(message, "status", f"Started thread {thread_id}.")]
        if response.action == "thread.attach":
            try:
                thread_id = await self.backend.attach_thread(message.channel_id, message.conversation_id, response.thread_id or "")
            except Exception as exc:
                return [self._message(message, "status", f"Thread {response.thread_id} could not be attached: {exc}.")]
            return [self._message(message, "status", f"Attached to thread {thread_id}.")]
        if response.action == "turn.stop":
            interrupted = await self.backend.interrupt_active_turn(message.channel_id, message.conversation_id)
            if not interrupted:
                return [self._message(message, "command_result", "No active turn to stop.")]
        elif response.action in {"approval.accept", "approval.deny", "approval.cancel"}:
            decision = {
                "approval.accept": "accept",
                "approval.deny": "decline",
                "approval.cancel": "cancel",
            }[response.action]
            try:
                await self.backend.reply_to_server_request(response.request_id or "", {"decision": decision})
            except (AppServerError, KeyError) as exc:
                return self._request_reply_failure(message, response.request_id, response.action, exc)
        elif response.action == "request.answer":
            payload = {"answers": {key: {"answers": value} for key, value in (response.answers or {}).items()}}
            try:
                await self.backend.reply_to_server_request(response.request_id or "", payload)
            except (AppServerError, KeyError) as exc:
                return self._request_reply_failure(message, response.request_id, response.action, exc)
        message_type = self._command_message_type(response.action)
        return [self._message(message, message_type, response.text, request_id=response.request_id)]

    async def handle_notification(self, notification: dict) -> list[OutboundMessage]:
        message = self.projector.project_notification(notification, self.store)
        return await self._emit(message)

    async def handle_server_request(self, request: dict) -> list[OutboundMessage]:
        message = self.projector.project_notification(request, self.store)
        return await self._emit(message)

    async def _emit(self, message: OutboundMessage | None) -> list[OutboundMessage]:
        if message is None:
            return []
        if self.outbound_sink is not None:
            await self.outbound_sink.send_message(message)
        return [message]

    async def _render_threads(self, message: InboundMessage, include_all: bool) -> str:
        threads = await self.backend.list_threads(message.channel_id, message.conversation_id, include_all=include_all)
        if not threads:
            return "Threads:\n(none)"
        lines = ["Threads:"]
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        for snapshot in threads:
            marker = "*" if snapshot.thread_id == binding.thread_id else "-"
            label = snapshot.name or snapshot.preview or snapshot.thread_id
            parts = [f"id: {snapshot.thread_id}", f"status: {snapshot.status}"]
            if include_all:
                parts.append(f"cwd: {snapshot.cwd}")
            lines.append(f"{marker} {label} ({', '.join(parts)})")
        return "\n".join(lines)

    async def _render_status(self, message: InboundMessage) -> str:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        cwd = self.store.current_cwd(message.channel_id, message.conversation_id) or "(none)"
        if binding.thread_id is None:
            return "\n".join(
                [
                    f"CWD: {cwd}",
                    "Thread: (none)",
                    "Thread ID: (none)",
                    "Thread Status: (none)",
                    f"Requests: {len(self.store.list_pending_requests(message.channel_id, message.conversation_id))} pending",
                ]
            )
        snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, binding.thread_id)
        if snapshot is None:
            return f"Current thread {binding.thread_id} is no longer available in Codex."
        active = self.store.get_active_turn(binding.thread_id)
        return "\n".join(
            [
                f"CWD: {snapshot.cwd or cwd}",
                f"Thread: {snapshot.name or snapshot.preview or snapshot.thread_id}",
                f"Thread ID: {snapshot.thread_id}",
                f"Thread Status: {snapshot.status}",
                f"Turn: {active[0] if active else '(none)'}",
                f"Turn Status: {active[1] if active else 'idle'}",
                f"Requests: {len(self.store.list_pending_requests(message.channel_id, message.conversation_id))} pending",
            ]
        )

    async def _render_thread(self, message: InboundMessage, thread_id: str | None) -> str:
        if thread_id is None:
            return "No active thread."
        try:
            snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, thread_id)
        except AppServerError as exc:
            return f"Current thread {thread_id} could not be queried from Codex right now: {exc}."
        if snapshot is None:
            return f"Current thread {thread_id} is no longer available in Codex."
        return "\n".join(
            [
                f"Thread: {snapshot.name or snapshot.preview or snapshot.thread_id}",
                f"Thread id: {snapshot.thread_id}",
                f"CWD: {snapshot.cwd or '(unknown)'}",
                f"Path: {snapshot.path or snapshot.cwd or '(unknown)'}",
                f"Status: {snapshot.status}",
            ]
        )

    def _message(
        self,
        inbound: InboundMessage,
        message_type: str,
        text: str,
        *,
        request_id: str | None = None,
    ) -> OutboundMessage:
        if message_type in _SYSTEM_MESSAGE_TYPES and not text.startswith(_SYSTEM_PREFIX):
            text = f"{_SYSTEM_PREFIX}{text}"
        return OutboundMessage(
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            message_type=message_type,
            text=text,
            request_id=request_id,
        )

    def _command_message_type(self, action: str) -> str:
        if action in {
            "project.cwd",
            "settings.view",
            "settings.visibility",
            "settings.model",
        }:
            return "status"
        if action.endswith(".invalid") or action.endswith(".missing") or ".missing" in action or action == "unknown":
            return "error"
        if action in {"thread.read.none", "turn.stop.none"}:
            return "command_result"
        return "command_result"

    def _request_reply_failure(
        self,
        inbound: InboundMessage,
        request_id: str | None,
        action: str,
        error: AppServerError | KeyError,
    ) -> list[OutboundMessage]:
        if self._is_expired_server_request_error(error):
            self.store.remove_pending_request(request_id or "")
            return [
                self._message(
                    inbound,
                    "status",
                    f"Request {request_id} is no longer pending.",
                    request_id=request_id,
                )
            ]
        verb = "approval" if action.startswith("approval.") else "answer"
        return [
            self._message(
                inbound,
                "status",
                f"Request {request_id} {verb} could not be sent to Codex right now. Try again.",
                request_id=request_id,
            )
        ]

    def _is_expired_server_request_error(self, error: AppServerError | KeyError) -> bool:
        if isinstance(error, KeyError):
            return True
        return "unknown pending request" in str(error).lower()

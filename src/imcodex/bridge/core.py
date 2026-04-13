from __future__ import annotations

import re
from typing import Any

from ..appserver import CodexBackend, StaleThreadBindingError
from ..models import InboundMessage, OutboundMessage
from ..store import ConversationStore
from .commands import CommandRouter, parse_command
from .projection import MessageProjector
from .request_registry import RequestRegistry
from .session_registry import SessionRegistry
from .thread_directory import ThreadDirectory
from .turn_state import TurnStateMachine


class BridgeService:
    def __init__(
        self,
        *,
        store: ConversationStore,
        backend: CodexBackend,
        command_router: CommandRouter,
        projector: MessageProjector,
        outbound_sink: Any | None = None,
        session_registry: SessionRegistry | None = None,
        thread_directory: ThreadDirectory | None = None,
        request_registry: RequestRegistry | None = None,
        turn_state: TurnStateMachine | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router
        self.projector = projector
        self.outbound_sink = outbound_sink
        self.session_registry = session_registry or SessionRegistry(store)
        self.thread_directory = thread_directory or ThreadDirectory(store)
        self.request_registry = request_registry or RequestRegistry(store)
        self.turn_state = turn_state or TurnStateMachine()
        if getattr(self.projector, "request_registry", None) is None:
            self.projector.request_registry = self.request_registry
        if getattr(self.projector, "turn_state", None) is None:
            self.projector.turn_state = self.turn_state

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        if message.text.startswith("/"):
            return await self._handle_command(message)
        return await self._handle_text(message)

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        native_messages = await self._handle_native_query_command(message)
        if native_messages is not None:
            return native_messages
        response = self.command_router.handle(message.channel_id, message.conversation_id, message.text)
        if response.action == "thread.new.missing_project":
            return [self._message(message, "error", response.text)]
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            label = self._thread_label(thread_id)
            if label == "Untitled thread":
                self.store.mark_pending_first_thread_label(message.channel_id, message.conversation_id, thread_id)
            return [self._message(message, "status", f"Started new thread {label} (id: {thread_id}).")]
        if response.action == "thread.attach":
            thread_id = await self.backend.attach_thread(
                message.channel_id,
                message.conversation_id,
                response.thread_id,
            )
            label = self._thread_label(thread_id)
            return [self._message(message, "status", f"Attached thread {label} (id: {thread_id}).")]
        if response.action == "turn.stop":
            await self.backend.interrupt_active_turn(message.channel_id, message.conversation_id)
        elif response.action.startswith("approval.") or response.action == "request.answer":
            payload = {"answers": {k: {"answers": v} for k, v in (response.answers or {}).items()}}
            ticket_ids = response.ticket_ids or ([response.ticket_id] if response.ticket_id else [])
            if response.action != "request.answer":
                decision = {
                    "approval.accept": "accept",
                    "approval.accept_session": "acceptForSession",
                    "approval.deny": "decline",
                    "approval.cancel": "cancel",
                }[response.action]
                payload = {"decision": decision}
            succeeded: list[str] = []
            failed: list[str] = []
            for ticket_id in ticket_ids:
                try:
                    await self.backend.reply_to_server_request(
                        message.channel_id,
                        message.conversation_id,
                        ticket_id,
                        payload,
                    )
                except Exception:
                    failed.append(ticket_id)
                else:
                    succeeded.append(ticket_id)
            if failed:
                parts = []
                if succeeded:
                    parts.append(f"Succeeded: {', '.join(succeeded)}.")
                parts.append(f"Failed: {', '.join(failed)}.")
                if response.missing_ticket_ids:
                    parts.append(f"Unknown tickets: {', '.join(response.missing_ticket_ids)}.")
                response.text = " ".join(parts)
        return [self._message(message, self._command_message_type(response.action), response.text, response.ticket_id)]

    async def _handle_text(self, message: InboundMessage) -> list[OutboundMessage]:
        selected_cwd = self._select_cwd(message.channel_id, message.conversation_id)
        if selected_cwd is None:
            return [
                self._message(
                    message,
                    "error",
                    "Choose a working directory first with /cwd <path>. You can still browse /projects and /project use <project-id>.",
                )
            ]
        prior_thread_id = self.store.get_binding(message.channel_id, message.conversation_id).active_thread_id
        try:
            await self.backend.start_turn(message.channel_id, message.conversation_id, message.text)
        except StaleThreadBindingError as exc:
            text = (
                f"Current thread {exc.thread_id} could not be resumed. "
                "Use /recover to clear the stale binding, /new, or /thread attach <thread-id>."
            )
            return [self._message(message, "status", text)]
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.active_thread_id is not None and binding.active_turn_id is not None:
            self.turn_state.start(binding.active_thread_id, binding.active_turn_id)
            self.turn_state.mark_in_progress(binding.active_thread_id, binding.active_turn_id)
        if binding.active_thread_id is not None and (
            binding.active_thread_id != prior_thread_id
            or self.store.consume_pending_first_thread_label(
                message.channel_id,
                message.conversation_id,
                binding.active_thread_id,
            )
        ):
            self.store.note_thread_user_message(binding.active_thread_id, message.text)
        return [self._message(message, "accepted", "Working on it.")]

    async def handle_notification(self, notification: dict[str, Any]) -> list[OutboundMessage]:
        result = self.projector.project_notification(notification, self.store)
        if result is None:
            return []
        messages = [result]
        if self.outbound_sink is not None:
            for outbound in messages:
                await self.outbound_sink.send_message(outbound)
        return messages

    async def handle_server_request(self, request: dict[str, Any]) -> list[OutboundMessage]:
        result = self.projector.project_notification(request, self.store)
        if result is None:
            return []
        messages = [result]
        if self.outbound_sink is not None:
            for outbound in messages:
                await self.outbound_sink.send_message(outbound)
        return messages

    def _select_cwd(self, channel_id: str, conversation_id: str) -> str | None:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.selected_cwd is not None:
            return binding.selected_cwd
        projects = self.store.list_projects()
        if len(projects) == 1:
            self.store.set_selected_cwd(channel_id, conversation_id, projects[0].cwd)
            return projects[0].cwd
        return None

    def _message(
        self,
        inbound: InboundMessage,
        message_type: str,
        text: str,
        ticket_id: str | None = None,
    ) -> OutboundMessage:
        return OutboundMessage(
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            message_type=message_type,
            text=text,
            ticket_id=ticket_id,
        )

    def _thread_label(self, thread_id: str) -> str:
        try:
            return self.store.thread_label(thread_id)
        except KeyError:
            return "Untitled thread"

    def _command_message_type(self, action: str) -> str:
        if action.startswith("settings.") or action in {
            "project.use",
            "project.cwd",
            "thread.use",
            "recover",
        }:
            return "status"
        if action.endswith(".missing") or action.endswith(".invalid"):
            return "error"
        return "command_result"

    async def _handle_native_query_command(self, message: InboundMessage) -> list[OutboundMessage] | None:
        try:
            command = parse_command(message.text)
        except ValueError:
            return None
        if command.name == "threads":
            include_all = "--all" in command.args
            binding = self.store.get_binding(message.channel_id, message.conversation_id)
            if not include_all and binding.selected_cwd is None:
                return None
            try:
                snapshots = await self.backend.list_threads(
                    message.channel_id,
                    message.conversation_id,
                    include_all=include_all,
                )
            except Exception:
                return None
            if include_all:
                lines = ["Threads across working directories:"]
            else:
                lines = [f"Threads for {binding.selected_cwd}:"]
            for snapshot in snapshots:
                marker = "*" if snapshot.thread_id == binding.active_thread_id else "-"
                label = snapshot.name or snapshot.preview or self._thread_label(snapshot.thread_id)
                parts = [
                    f"id: {snapshot.thread_id}",
                    f"status: {self._humanize_status(snapshot.status or 'idle')}",
                ]
                if include_all or binding.selected_cwd is None:
                    parts.append(f"cwd: {snapshot.cwd}")
                lines.append(f"{marker} {label} ({', '.join(parts)})")
            return [self._message(message, "command_result", "\n".join(lines))]
        if command.name == "thread" and command.args == ["read"]:
            binding = self.store.get_binding(message.channel_id, message.conversation_id)
            if binding.active_thread_id is None:
                return [self._message(message, "command_result", "No active thread.")]
            try:
                snapshot = await self.backend.read_thread(
                    message.channel_id,
                    message.conversation_id,
                    binding.active_thread_id,
                )
            except Exception:
                text = (
                    f"Current thread {binding.active_thread_id} could not be validated. "
                    "Use /recover, /new, or /thread attach <thread-id>."
                )
                return [self._message(message, "status", text)]
            if snapshot is None:
                return [self._message(message, "command_result", "No active thread.")]
            text = "\n".join(
                [
                    f"Thread: {snapshot.name or snapshot.preview or self._thread_label(snapshot.thread_id)}",
                    f"Thread id: {snapshot.thread_id}",
                    f"CWD: {snapshot.cwd or '(unknown)'}",
                    f"Status: {self._humanize_status(snapshot.status or '(unknown)')}",
                ]
            )
            return [self._message(message, "command_result", text)]
        return None

    def _humanize_status(self, status: str) -> str:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", status)
        return spaced.replace("_", " ").strip().lower()

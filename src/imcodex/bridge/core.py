from __future__ import annotations

from typing import Any

from ..appserver import CodexBackend
from ..models import InboundMessage, OutboundMessage
from ..store import ConversationStore
from .commands import CommandRouter
from .projection import MessageProjector


class BridgeService:
    def __init__(
        self,
        *,
        store: ConversationStore,
        backend: CodexBackend,
        command_router: CommandRouter,
        projector: MessageProjector,
        outbound_sink: Any | None = None,
        auto_approve_mode: str | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router
        self.projector = projector
        self.outbound_sink = outbound_sink
        self.auto_approve_mode = auto_approve_mode

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        if message.text.startswith("/"):
            return await self._handle_command(message)
        return await self._handle_text(message)

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        response = self.command_router.handle(message.channel_id, message.conversation_id, message.text)
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            label = self._thread_label(thread_id)
            if label == "Untitled thread":
                self.store.mark_pending_first_thread_label(message.channel_id, message.conversation_id, thread_id)
            return [self._message(message, "status", f"Started new thread {label} (id: {thread_id}).")]
        if response.action == "turn.stop":
            await self.backend.interrupt_active_turn(message.channel_id, message.conversation_id)
        elif response.action.startswith("approval.") or response.action == "request.answer":
            ticket_id = response.ticket_id
            if ticket_id:
                payload = {"answers": {k: {"answers": v} for k, v in (response.answers or {}).items()}}
                if response.action != "request.answer":
                    decision = {
                        "approval.accept": "accept",
                        "approval.accept_session": "acceptForSession",
                        "approval.deny": "decline",
                        "approval.cancel": "cancel",
                    }[response.action]
                    payload = {"decision": decision}
                await self.backend.reply_to_server_request(ticket_id, payload)
        return [self._message(message, "command_result", response.text, response.ticket_id)]

    async def _handle_text(self, message: InboundMessage) -> list[OutboundMessage]:
        project = self._select_project(message.channel_id, message.conversation_id)
        if project is None:
            return [
                self._message(
                    message,
                    "error",
                    "Choose a working directory first with /cwd <path>. You can still browse /projects and /project use <project-id>.",
                )
            ]
        prior_thread_id = self.store.get_binding(message.channel_id, message.conversation_id).active_thread_id
        await self.backend.start_turn(message.channel_id, message.conversation_id, message.text)
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
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
        if (
            self.auto_approve_mode is not None
            and request.get("method") in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
            and result.ticket_id is not None
        ):
            await self.backend.reply_to_server_request(
                result.ticket_id,
                {"decision": self.auto_approve_mode},
            )
            return []
        messages = [result]
        if self.outbound_sink is not None:
            for outbound in messages:
                await self.outbound_sink.send_message(outbound)
        return messages

    def _select_project(self, channel_id: str, conversation_id: str):
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_project_id is not None:
            return self.store.get_project(binding.active_project_id)
        projects = self.store.list_projects()
        if len(projects) == 1:
            self.store.set_active_project(channel_id, conversation_id, projects[0].project_id)
            return projects[0]
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

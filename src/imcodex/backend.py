from __future__ import annotations

import asyncio

from .appserver_client import AppServerError
from .store import ConversationStore


class CodexBackend:
    def __init__(self, *, client, store: ConversationStore, service_name: str) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name

    async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_thread_id:
            return binding.active_thread_id
        project = self.store.get_project(binding.active_project_id)
        result = await self.client.start_thread(
            cwd=project.cwd,
            approval_policy=None,
            sandbox=None,
            model=None,
            personality="friendly",
            service_name=self.service_name,
        )
        thread_id = result["thread"]["id"]
        self.store.record_thread(thread_id=thread_id, cwd=project.cwd, preview=result["thread"].get("preview", ""))
        self.store.set_active_thread(channel_id, conversation_id, thread_id)
        return thread_id

    async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
        self.store.clear_active_thread(channel_id, conversation_id)
        return await self.ensure_thread(channel_id, conversation_id)

    async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        if (
            binding.active_thread_id is not None
            and binding.active_turn_id is not None
            and binding.active_turn_status == "inProgress"
        ):
            try:
                await self._steer_active_turn(
                    thread_id=binding.active_thread_id,
                    turn_id=binding.active_turn_id,
                    text=text,
                )
            except AppServerError as exc:
                if not self._should_recover_from_steer_failure(exc):
                    raise
                await self._interrupt_best_effort(binding.active_thread_id, binding.active_turn_id)
                self.store.clear_active_turn(channel_id, conversation_id)
            else:
                self.store.set_active_turn(
                    channel_id,
                    conversation_id,
                    thread_id=binding.active_thread_id,
                    turn_id=binding.active_turn_id,
                    status="inProgress",
                )
                return binding.active_turn_id
        had_bound_thread = binding.active_thread_id is not None
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        try:
            result = await self._start_turn(thread_id, text)
        except AppServerError:
            if not had_bound_thread:
                raise
            self.store.clear_active_thread(channel_id, conversation_id)
            thread_id = await self.ensure_thread(channel_id, conversation_id)
            result = await self._start_turn(thread_id, text)
        turn_id = result["turn"]["id"]
        self.store.set_active_turn(
            channel_id,
            conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            status=result["turn"].get("status", "inProgress"),
        )
        return turn_id

    async def _start_turn(self, thread_id: str, text: str):
        return await self.client.start_turn(
            thread_id=thread_id,
            text=text,
            cwd=None,
            model=None,
            approval_policy=None,
            sandbox_policy=None,
            effort=None,
            summary="concise",
        )

    async def interrupt_active_turn(self, channel_id: str, conversation_id: str) -> None:
        binding = self.store.get_binding(channel_id, conversation_id)
        if not binding.active_thread_id or not binding.active_turn_id:
            return
        await self._interrupt_if_possible(binding.active_thread_id, binding.active_turn_id)
        self.store.clear_active_turn(channel_id, conversation_id)

    async def reply_to_server_request(self, ticket_id: str, decision_or_answers: dict) -> None:
        request = self.store.get_pending_request(ticket_id)
        if request is None:
            raise KeyError(ticket_id)
        client_ticket_id = request.request_id or ticket_id
        await self.client.reply_to_server_request(client_ticket_id, decision_or_answers)
        self.store.resolve_pending_request(ticket_id, decision_or_answers)

    async def _interrupt_if_possible(self, thread_id: str, turn_id: str) -> None:
        await self.client.interrupt_turn(
            thread_id=thread_id,
            turn_id=turn_id,
        )

    async def _interrupt_best_effort(self, thread_id: str, turn_id: str) -> None:
        try:
            await self._interrupt_if_possible(thread_id, turn_id)
        except AppServerError:
            return

    async def _steer_active_turn(self, *, thread_id: str, turn_id: str, text: str) -> None:
        for attempt in range(2):
            try:
                await self.client.steer_turn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    text=text,
                )
                return
            except AppServerError as exc:
                if attempt == 0 and self._should_retry_steer(exc):
                    await asyncio.sleep(0.05)
                    continue
                raise

    def _should_recover_from_steer_failure(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return "invalid request" in message or "no active turn" in message

    def _should_retry_steer(self, error: AppServerError) -> bool:
        return "no active turn" in str(error).lower()

from __future__ import annotations

import asyncio

from .client import AppServerError
from ..bridge.thread_directory import NativeThreadSnapshot, ThreadDirectory
from ..store import ConversationStore


class StaleThreadBindingError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"thread binding is stale: {thread_id}")


class CodexBackend:
    def __init__(self, *, client, store: ConversationStore, service_name: str) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name
        self.thread_directory = ThreadDirectory(store)

    async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        cwd = self._binding_cwd(binding)
        if binding.active_thread_id:
            try:
                result = await self.client.resume_thread(
                    thread_id=binding.active_thread_id,
                    **self._thread_session_params(cwd, binding.permission_profile),
                )
            except AppServerError as exc:
                if not self._should_mark_thread_stale(exc):
                    raise
                self.store.note_thread_status(binding.active_thread_id, status="stale")
                raise StaleThreadBindingError(binding.active_thread_id) from exc
            else:
                thread_id = result["thread"]["id"]
                self._bind_thread_result(
                    channel_id=channel_id,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    cwd=cwd,
                    preview=result["thread"].get("preview", ""),
                )
                return thread_id
        result = await self.client.start_thread(
            **self._thread_session_params(cwd, binding.permission_profile)
        )
        thread_id = result["thread"]["id"]
        self._bind_thread_result(
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            cwd=cwd,
            preview=result["thread"].get("preview", ""),
        )
        return thread_id

    async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
        self.store.clear_active_thread(channel_id, conversation_id)
        return await self.ensure_thread(channel_id, conversation_id)

    async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        cwd = self._binding_cwd(binding)
        result = await self.client.resume_thread(
            thread_id=thread_id,
            **self._thread_session_params(cwd, binding.permission_profile),
        )
        resolved_thread_id = result["thread"]["id"]
        self._bind_thread_result(
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_id=resolved_thread_id,
            cwd=cwd,
            preview=result["thread"].get("preview", ""),
        )
        return resolved_thread_id

    async def list_threads(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        include_all: bool = False,
    ) -> list[NativeThreadSnapshot]:
        result = await self.client.list_threads()
        snapshots = self.thread_directory.import_threads(
            [self._normalize_thread_payload(item) for item in result.get("threads", [])]
        )
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.selected_cwd and not include_all:
            normalized = self.thread_directory._normalize_cwd(binding.selected_cwd)
            return [
                snapshot
                for snapshot in snapshots
                if self.thread_directory._normalize_cwd(snapshot.cwd) == normalized
            ]
        return snapshots

    async def read_thread(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> NativeThreadSnapshot | None:
        del channel_id, conversation_id
        result = await self.client.read_thread(thread_id)
        payload = result.get("thread")
        if not isinstance(payload, dict):
            return None
        normalized = self._normalize_thread_payload(payload)
        return self.thread_directory.remember_thread(
            thread_id=str(normalized["id"]),
            cwd=str(normalized["cwd"]),
            preview=str(normalized["preview"]),
            status=str(normalized["status"]),
            name=normalized.get("name"),
            path=normalized.get("path"),
        )

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
                self.store.clear_pending_requests_for_turn(
                    channel_id=channel_id,
                    conversation_id=conversation_id,
                    thread_id=binding.active_thread_id,
                    turn_id=binding.active_turn_id,
                )
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
            result = await self._start_turn(thread_id, text, binding.permission_profile)
        except AppServerError:
            if not had_bound_thread:
                raise
            self.store.clear_active_thread(channel_id, conversation_id)
            thread_id = await self.ensure_thread(channel_id, conversation_id)
            rebound = self.store.get_binding(channel_id, conversation_id)
            result = await self._start_turn(thread_id, text, rebound.permission_profile)
        turn_id = result["turn"]["id"]
        self.store.set_active_turn(
            channel_id,
            conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            status=result["turn"].get("status", "inProgress"),
        )
        return turn_id

    async def _start_turn(self, thread_id: str, text: str, permission_profile: str):
        return await self.client.start_turn(
            thread_id=thread_id,
            text=text,
            cwd=None,
            model=None,
            approval_policy=self._approval_policy(permission_profile),
            sandbox_policy=None,
            effort=None,
            summary="concise",
        )

    async def interrupt_active_turn(self, channel_id: str, conversation_id: str) -> None:
        binding = self.store.get_binding(channel_id, conversation_id)
        if not binding.active_thread_id or not binding.active_turn_id:
            return
        self.store.clear_pending_requests_for_turn(
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_id=binding.active_thread_id,
            turn_id=binding.active_turn_id,
        )
        await self._interrupt_if_possible(binding.active_thread_id, binding.active_turn_id)
        self.store.clear_active_turn(channel_id, conversation_id)

    async def reply_to_server_request(
        self,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
        decision_or_answers: dict,
    ) -> None:
        request = self.store.get_pending_request(
            ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if request is None:
            raise KeyError(ticket_id)
        client_ticket_id = request.request_id or ticket_id
        await self.client.reply_to_server_request(client_ticket_id, decision_or_answers)
        self.store.mark_pending_request_submitted(
            ticket_id,
            decision_or_answers,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )

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

    def _should_mark_thread_stale(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return (
            "invalid request" in message
            or "not found" in message
            or "unknown thread" in message
            or "no such thread" in message
        )

    def _thread_session_params(self, cwd: str, permission_profile: str) -> dict[str, str | None]:
        return {
            "cwd": cwd,
            "approval_policy": self._approval_policy(permission_profile),
            "sandbox": None,
            "model": None,
            "personality": "friendly",
            "service_name": self.service_name,
        }

    def _approval_policy(self, permission_profile: str) -> str | None:
        if permission_profile == "autonomous":
            return "never"
        return None

    def _binding_cwd(self, binding) -> str:
        if binding.active_project_id is not None:
            return self.store.get_project(binding.active_project_id).cwd
        if binding.active_thread_id is not None:
            return self.store.get_thread(binding.active_thread_id).cwd
        projects = self.store.list_projects()
        if len(projects) == 1:
            return projects[0].cwd
        raise KeyError("No working directory selected for thread session")

    def _bind_thread_result(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        cwd: str,
        preview: str,
    ) -> None:
        self.store.record_thread(thread_id=thread_id, cwd=cwd, preview=preview)
        self.store.set_active_thread(channel_id, conversation_id, thread_id)

    def _normalize_thread_payload(self, payload: dict) -> dict[str, str | None]:
        status = payload.get("status")
        if isinstance(status, dict):
            status = status.get("type") or status.get("status")
        cwd = payload.get("cwd") or payload.get("path") or ""
        return {
            "id": str(payload.get("id") or payload.get("threadId") or ""),
            "cwd": str(cwd),
            "path": str(payload.get("path") or cwd or ""),
            "preview": str(payload.get("preview") or ""),
            "status": str(status or "idle"),
            "name": payload.get("name"),
        }

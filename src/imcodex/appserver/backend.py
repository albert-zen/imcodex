from __future__ import annotations

from dataclasses import dataclass

from ..models import NativeThreadSnapshot
from ..store import ConversationStore
from .client import AppServerError


class StaleThreadBindingError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"thread binding is stale: {thread_id}")


@dataclass(slots=True)
class TurnSubmission:
    kind: str
    thread_id: str
    turn_id: str


class CodexBackend:
    def __init__(self, *, client, store: ConversationStore, service_name: str) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name

    async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
        self.store.clear_thread_binding(channel_id, conversation_id)
        return await self.ensure_thread(channel_id, conversation_id)

    async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id:
            try:
                result = await self.client.resume_thread(
                    thread_id=binding.thread_id,
                    service_name=self.service_name,
                    personality="friendly",
                )
            except AppServerError as exc:
                if self._is_stale_thread_error(exc):
                    raise StaleThreadBindingError(binding.thread_id) from exc
                raise
            snapshot = self._remember_snapshot(result.get("thread") or {})
            self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
            return snapshot.thread_id
        if binding.bootstrap_cwd is None:
            raise KeyError("No working directory selected for thread session")
        result = await self.client.start_thread(
            cwd=binding.bootstrap_cwd,
            service_name=self.service_name,
            personality="friendly",
        )
        snapshot = self._remember_snapshot(result.get("thread") or {})
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        return snapshot.thread_id

    async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
        snapshot = await self.read_thread(channel_id, conversation_id, thread_id)
        if snapshot is None:
            raise AppServerError(f"thread {thread_id} is not available in Codex")
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        return snapshot.thread_id

    async def list_threads(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        include_all: bool = False,
    ) -> list[NativeThreadSnapshot]:
        params: dict[str, str] = {}
        cwd = self.store.current_cwd(channel_id, conversation_id)
        if cwd and not include_all:
            params["cwd"] = cwd
        result = await self.client.list_threads(**params)
        return [self._remember_snapshot(item) for item in result.get("threads", [])]

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
        return self._remember_snapshot(payload)

    async def submit_text(self, channel_id: str, conversation_id: str, text: str) -> TurnSubmission:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is not None:
            active = self.store.get_active_turn(binding.thread_id)
            if active is not None and active[1] == "inProgress":
                try:
                    await self.client.steer_turn(binding.thread_id, active[0], text)
                except AppServerError as exc:
                    if not self._is_stale_turn_error(exc):
                        raise
                    self.store.clear_active_turn(binding.thread_id)
                else:
                    return TurnSubmission(kind="steer", thread_id=binding.thread_id, turn_id=active[0])
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        model_override = self.store.pop_next_model_override(channel_id, conversation_id)
        result = await self.client.start_turn(thread_id=thread_id, text=text, model=model_override, summary="concise")
        turn = result.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        status = str(turn.get("status") or "inProgress")
        self.store.note_active_turn(thread_id, turn_id, status)
        return TurnSubmission(kind="start", thread_id=thread_id, turn_id=turn_id)

    async def interrupt_active_turn(self, channel_id: str, conversation_id: str) -> bool:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return False
        active = self.store.get_active_turn(binding.thread_id)
        if active is None:
            return False
        try:
            await self.client.interrupt_turn(binding.thread_id, active[0])
        except AppServerError as exc:
            if not self._is_stale_turn_error(exc):
                raise
            self.store.suppress_turn(binding.thread_id, active[0])
            self.store.clear_active_turn(binding.thread_id)
            self.store.remove_pending_requests_for_turn(binding.thread_id, active[0])
            return False
        self.store.suppress_turn(binding.thread_id, active[0])
        self.store.clear_active_turn(binding.thread_id)
        self.store.remove_pending_requests_for_turn(binding.thread_id, active[0])
        return True

    async def reply_to_server_request(self, request_id: str, decision_or_answers: dict) -> None:
        await self.client.reply_to_server_request(request_id, decision_or_answers)

    def _remember_snapshot(self, payload: dict) -> NativeThreadSnapshot:
        status = payload.get("status")
        if isinstance(status, dict):
            status = status.get("type") or status.get("status")
        snapshot = NativeThreadSnapshot(
            thread_id=str(payload.get("id") or payload.get("threadId") or ""),
            cwd=str(payload.get("cwd") or ""),
            preview=str(payload.get("preview") or ""),
            status=str(status or "idle"),
            name=str(payload["name"]) if payload.get("name") is not None else None,
            path=str(payload["path"]) if payload.get("path") is not None else None,
        )
        self.store.note_thread_snapshot(snapshot)
        return snapshot

    def _is_stale_thread_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in ("invalid request", "not found", "unknown thread", "no such thread"))

    def _is_stale_turn_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "no active turn",
                "unknown turn",
                "no such turn",
                "turn not found",
                "expected turn",
            )
        )

from __future__ import annotations

from dataclasses import dataclass
import logging

from ..store import ConversationStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionRecord:
    channel_id: str
    conversation_id: str
    selected_cwd: str | None
    thread_id: str | None
    active_turn_id: str | None
    active_turn_status: str | None
    last_inbound_message_id: str | None
    visibility_profile: str
    pending_request_ids: list[str]
    permission_profile: str
    show_commentary: bool
    show_toolcalls: bool
    last_seen_thread_name: str | None
    last_seen_thread_path: str | None
    last_seen_thread_status: str | None


class SessionRegistry:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store
        self._thread_sessions: dict[str, tuple[str, str]] = {}
        self._session_threads: dict[tuple[str, str], str] = {}
        self._hydrate_from_store()

    def get(self, channel_id: str, conversation_id: str) -> SessionRecord:
        binding = self.store.get_binding(channel_id, conversation_id)
        return SessionRecord(
            channel_id=channel_id,
            conversation_id=conversation_id,
            selected_cwd=binding.selected_cwd,
            thread_id=binding.active_thread_id,
            active_turn_id=binding.active_turn_id,
            active_turn_status=binding.active_turn_status,
            last_inbound_message_id=binding.last_inbound_message_id,
            visibility_profile=binding.visibility_profile,
            pending_request_ids=list(binding.pending_request_ids),
            permission_profile=binding.permission_profile,
            show_commentary=binding.show_commentary,
            show_toolcalls=binding.show_toolcalls,
            last_seen_thread_name=binding.last_seen_thread_name,
            last_seen_thread_path=binding.last_seen_thread_path,
            last_seen_thread_status=binding.last_seen_thread_status,
        )

    def get_by_thread(self, thread_id: str) -> SessionRecord | None:
        binding = self.find_binding(thread_id)
        if binding is None:
            return None
        return self.get(binding.channel_id, binding.conversation_id)

    def find_binding(self, thread_id: str):
        key = self._thread_sessions.get(thread_id)
        if key is not None:
            binding = self.store.get_binding(*key)
            if binding.active_thread_id == thread_id:
                return binding
            self._drop_runtime_thread(thread_id, key)
        return None

    def find_routing_binding(self, thread_id: str):
        return self.find_binding(thread_id)

    def sync(self, channel_id: str, conversation_id: str) -> SessionRecord:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_thread_id:
            self._bind_runtime((channel_id, conversation_id), binding.active_thread_id)
        else:
            self._drop_runtime_session((channel_id, conversation_id))
        return self.get(channel_id, conversation_id)

    def bind_cwd(self, channel_id: str, conversation_id: str, cwd: str) -> SessionRecord:
        self.store.set_selected_cwd(channel_id, conversation_id, cwd)
        return self.sync(channel_id, conversation_id)

    def bind_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> SessionRecord:
        self.store.set_active_thread(channel_id, conversation_id, thread_id)
        self._bind_runtime((channel_id, conversation_id), thread_id)
        return self.get(channel_id, conversation_id)

    def note_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> SessionRecord | None:
        key = self._thread_sessions.get(thread_id)
        if key is not None:
            binding = self.store.get_binding(*key)
            if binding.active_thread_id != thread_id:
                self._drop_runtime_thread(thread_id, key)
                key = None
        if key is None:
            self.store.note_turn_started(thread_id, turn_id=turn_id, status=status)
            return None
        binding = self.store.set_active_turn(
            key[0],
            key[1],
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
        )
        thread = self.store._threads.get(thread_id)
        if thread is not None:
            thread.status = status
        binding.last_seen_thread_status = status
        self._bind_runtime(key, thread_id)
        return self.get(*key)

    def note_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> SessionRecord | None:
        key = self._thread_sessions.get(thread_id)
        if key is not None:
            binding = self.store.get_binding(*key)
            if binding.active_thread_id != thread_id:
                self._drop_runtime_thread(thread_id, key)
                key = None
        if key is None:
            self.store.note_turn_completed(thread_id, turn_id=turn_id, status=status)
            return None
        thread = self.store._threads.get(thread_id)
        binding = self.store.get_binding(*key)
        if thread is not None:
            thread.status = status
        stored_turn = self.store._thread_active_turns.get(thread_id)
        should_update_last_turn = (
            thread is None
            or thread.last_turn_id is None
            or thread.last_turn_id == turn_id
            or (stored_turn is not None and stored_turn[0] == turn_id)
            or binding.active_turn_id == turn_id
        )
        if should_update_last_turn and thread is not None:
            thread.last_turn_id = turn_id
            thread.last_turn_status = status
        if stored_turn is not None and stored_turn[0] == turn_id:
            self.store._thread_active_turns.pop(thread_id, None)
        if binding.active_thread_id == thread_id:
            if binding.active_turn_id == turn_id:
                binding.active_turn_id = None
                binding.active_turn_status = status
                binding.last_seen_thread_status = status
            elif binding.active_turn_id is None:
                binding.active_turn_status = status
                binding.last_seen_thread_status = status
        self._bind_runtime(key, thread_id)
        return self.get(*key)

    def _bind_runtime(self, key: tuple[str, str], thread_id: str) -> None:
        previous_thread = self._session_threads.get(key)
        if previous_thread is not None and previous_thread != thread_id:
            self._drop_runtime_thread(previous_thread, key)
        previous_key = self._thread_sessions.get(thread_id)
        if previous_key is not None and previous_key != key:
            self._release_runtime_owner(thread_id, previous_key)
            self._drop_runtime_thread(thread_id, previous_key)
        self._session_threads[key] = thread_id
        self._thread_sessions[thread_id] = key

    def _hydrate_from_store(self) -> None:
        for binding in self.store.iter_bindings():
            if not binding.active_thread_id:
                continue
            logger.debug(
                "Hydrating runtime thread binding thread_id=%s channel_id=%s conversation_id=%s",
                binding.active_thread_id,
                binding.channel_id,
                binding.conversation_id,
            )
            self._bind_runtime(
                (binding.channel_id, binding.conversation_id),
                binding.active_thread_id,
            )

    def _drop_runtime_session(self, key: tuple[str, str]) -> None:
        previous_thread = self._session_threads.get(key)
        if previous_thread is not None:
            self._drop_runtime_thread(previous_thread, key)

    def _drop_runtime_thread(self, thread_id: str, key: tuple[str, str]) -> None:
        if self._thread_sessions.get(thread_id) == key:
            self._thread_sessions.pop(thread_id, None)
        if self._session_threads.get(key) == thread_id:
            self._session_threads.pop(key, None)

    def _release_runtime_owner(
        self,
        thread_id: str,
        source_key: tuple[str, str],
    ) -> None:
        source_binding = self.store.get_binding(*source_key)
        if source_binding.active_thread_id == thread_id:
            source_binding.active_thread_id = None
            source_binding.active_turn_id = None
            source_binding.active_turn_status = None

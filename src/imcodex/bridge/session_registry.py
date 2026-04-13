from __future__ import annotations

from dataclasses import dataclass

from ..store import ConversationStore


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
        return self.store.find_binding_for_thread(thread_id)

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
            binding = self.store.note_turn_started(thread_id, turn_id=turn_id, status=status)
            if binding is None:
                return None
            if binding.active_thread_id == thread_id:
                self._bind_runtime((binding.channel_id, binding.conversation_id), thread_id)
            return self.get(binding.channel_id, binding.conversation_id)
        try:
            binding = self.store.set_active_turn(
                key[0],
                key[1],
                thread_id=thread_id,
                turn_id=turn_id,
                status=status,
            )
            thread = self.store.get_thread(thread_id)
        except KeyError:
            return None
        thread.status = status
        binding.last_seen_thread_status = status
        self.store._save()
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
            binding = self.store.note_turn_completed(thread_id, turn_id=turn_id, status=status)
            if binding is None:
                return None
            if binding.active_thread_id == thread_id:
                self._bind_runtime((binding.channel_id, binding.conversation_id), thread_id)
            return self.get(binding.channel_id, binding.conversation_id)
        try:
            thread = self.store.get_thread(thread_id)
            binding = self.store.get_binding(*key)
        except KeyError:
            return None
        thread.status = status
        stored_turn = self.store._thread_active_turns.get(thread_id)
        should_update_last_turn = (
            thread.last_turn_id is None
            or thread.last_turn_id == turn_id
            or (stored_turn is not None and stored_turn[0] == turn_id)
            or binding.active_turn_id == turn_id
        )
        if should_update_last_turn:
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
        self.store._save()
        self._bind_runtime(key, thread_id)
        return self.get(*key)

    def _bind_runtime(self, key: tuple[str, str], thread_id: str) -> None:
        previous_thread = self._session_threads.get(key)
        if previous_thread is not None and previous_thread != thread_id:
            self._drop_runtime_thread(previous_thread, key)
        previous_key = self._thread_sessions.get(thread_id)
        if previous_key is not None and previous_key != key:
            self._move_thread_runtime_state(thread_id, previous_key, key)
            self._drop_runtime_thread(thread_id, previous_key)
        self._session_threads[key] = thread_id
        self._thread_sessions[thread_id] = key

    def _drop_runtime_session(self, key: tuple[str, str]) -> None:
        previous_thread = self._session_threads.get(key)
        if previous_thread is not None:
            self._drop_runtime_thread(previous_thread, key)

    def _drop_runtime_thread(self, thread_id: str, key: tuple[str, str]) -> None:
        if self._thread_sessions.get(thread_id) == key:
            self._thread_sessions.pop(thread_id, None)
        if self._session_threads.get(key) == thread_id:
            self._session_threads.pop(key, None)

    def _move_thread_runtime_state(
        self,
        thread_id: str,
        source_key: tuple[str, str],
        target_key: tuple[str, str],
    ) -> None:
        source_binding = self.store.get_binding(*source_key)
        target_binding = self.store.get_binding(*target_key)
        if source_binding.active_thread_id == thread_id:
            source_binding.active_thread_id = None
            source_binding.active_turn_id = None
            source_binding.active_turn_status = None
        source_label_key = (source_key[0], source_key[1])
        target_label_key = (target_key[0], target_key[1])
        if self.store._pending_first_thread_labels.get(source_label_key) == thread_id:
            self.store._pending_first_thread_labels.pop(source_label_key, None)
            self.store._pending_first_thread_labels[target_label_key] = thread_id
        moved_ticket_ids: list[tuple[str, str]] = []
        for request_key, request in list(self.store._pending_requests.items()):
            if request.channel_id != source_key[0] or request.conversation_id != source_key[1]:
                continue
            if request.thread_id != thread_id:
                continue
            original_ticket_id = request.ticket_id
            self.store._pending_requests.pop(request_key, None)
            request.channel_id = target_key[0]
            request.conversation_id = target_key[1]
            target_ticket_id = original_ticket_id
            target_request_key = self.store._pending_request_key(target_key[0], target_key[1], target_ticket_id)
            while target_request_key in self.store._pending_requests:
                target_ticket_id = self.store.next_ticket_id(target_key[0], target_key[1])
                request.ticket_id = target_ticket_id
                target_request_key = self.store._pending_request_key(target_key[0], target_key[1], target_ticket_id)
            self.store._pending_requests[target_request_key] = request
            moved_ticket_ids.append((original_ticket_id, target_ticket_id))
            if target_ticket_id.isdigit():
                target_binding.next_ticket = max(target_binding.next_ticket, int(target_ticket_id) + 1)
        for source_ticket_id, target_ticket_id in moved_ticket_ids:
            if source_ticket_id in source_binding.pending_request_ids:
                source_binding.pending_request_ids.remove(source_ticket_id)
            if target_ticket_id not in target_binding.pending_request_ids:
                target_binding.pending_request_ids.append(target_ticket_id)
        self.store._save()

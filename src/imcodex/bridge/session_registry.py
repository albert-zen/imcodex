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

    def bind_cwd(self, channel_id: str, conversation_id: str, cwd: str) -> SessionRecord:
        self.store.set_selected_cwd(channel_id, conversation_id, cwd)
        return self.get(channel_id, conversation_id)

    def bind_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> SessionRecord:
        self.store.set_active_thread(channel_id, conversation_id, thread_id)
        return self.get(channel_id, conversation_id)

    def note_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> SessionRecord | None:
        binding = self.store.note_turn_started(thread_id, turn_id=turn_id, status=status)
        if binding is None:
            return None
        return self.get(binding.channel_id, binding.conversation_id)

    def note_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> SessionRecord | None:
        binding = self.store.note_turn_completed(thread_id, turn_id=turn_id, status=status)
        if binding is None:
            return None
        return self.get(binding.channel_id, binding.conversation_id)

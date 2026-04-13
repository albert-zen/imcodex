from __future__ import annotations

import os
import json
import textwrap
from pathlib import Path
from typing import Any, Callable

from .models import ConversationBinding, PendingRequest, ThreadRecord


Clock = Callable[[], float]


def _normalize_cwd(cwd: str) -> str:
    return os.path.normcase(os.path.normpath(cwd))


def _clip_thread_label(text: str) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    shortened = textwrap.shorten(collapsed, width=60, placeholder="...")
    if shortened != "...":
        return shortened
    return f"{collapsed[:57].rstrip()}..."


class ConversationStore:
    def __init__(
        self,
        clock: Clock,
        state_path: str | Path | None = None,
        *,
        default_permission_profile: str = "review",
    ):
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self.default_permission_profile = default_permission_profile
        self._threads: dict[str, ThreadRecord] = {}
        self._thread_active_turns: dict[str, tuple[str, str]] = {}
        self._thread_first_user_messages: dict[str, str] = {}
        self._pending_first_thread_labels: dict[tuple[str, str], str] = {}
        self._thread_order: list[str] = []
        self._bindings: dict[tuple[str, str], ConversationBinding] = {}
        self._pending_requests: dict[str, PendingRequest] = {}
        self._seq = 0
        if self.state_path and self.state_path.exists():
            self._load()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        preview: str,
        status: str = "idle",
        name: str | None = None,
        path: str | None = None,
    ) -> ThreadRecord:
        existing = self._threads.get(thread_id)
        thread = ThreadRecord(
            thread_id=thread_id,
            preview=preview or (existing.preview if existing is not None else ""),
            status=status,
            last_used_at=self.clock(),
            cwd=cwd,
            name=name if name is not None else (existing.name if existing is not None else None),
            path=path if path is not None else (existing.path if existing is not None else None),
            last_turn_id=existing.last_turn_id if existing is not None else None,
            last_turn_status=existing.last_turn_status if existing is not None else None,
            stale_turn_ids=list(existing.stale_turn_ids) if existing is not None else [],
            created_seq=existing.created_seq if existing is not None else self._next_seq(),
        )
        self._threads[thread_id] = thread
        if thread_id not in self._thread_order:
            self._thread_order.append(thread_id)
        self._save()
        return thread

    def set_selected_cwd(
        self,
        channel_id: str,
        conversation_id: str,
        cwd: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=None)
        binding.selected_cwd = cwd
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        self._save()
        return binding

    def get_thread(self, thread_id: str) -> ThreadRecord:
        return self._threads[thread_id]

    def note_thread_user_message(self, thread_id: str, text: str) -> None:
        self.get_thread(thread_id)
        if thread_id in self._thread_first_user_messages:
            return
        clipped = _clip_thread_label(text)
        if not clipped:
            return
        self._thread_first_user_messages[thread_id] = clipped
        self._save()

    def thread_label(self, thread_id: str) -> str:
        thread = self.get_thread(thread_id)
        if thread.name:
            label = _clip_thread_label(thread.name)
            if label:
                return label
        preview = _clip_thread_label(thread.preview)
        if preview:
            return preview
        first_user_message = self._thread_first_user_messages.get(thread_id, "")
        if first_user_message:
            return first_user_message
        return "Untitled thread"

    def note_thread_status(self, thread_id: str, *, status: str) -> ThreadRecord | None:
        try:
            thread = self.get_thread(thread_id)
        except KeyError:
            return None
        thread.status = status
        binding = self.find_binding_for_thread(thread_id)
        if binding is not None and binding.active_thread_id == thread_id:
            binding.last_seen_thread_status = status
        self._save()
        return thread

    def note_thread_name(self, thread_id: str, *, name: str) -> ThreadRecord | None:
        try:
            thread = self.get_thread(thread_id)
        except KeyError:
            return None
        thread.name = name
        binding = self.find_binding_for_thread(thread_id)
        if binding is not None and binding.active_thread_id == thread_id:
            binding.last_seen_thread_name = self.thread_label(thread_id)
        self._save()
        return thread

    def mark_pending_first_thread_label(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> None:
        self._pending_first_thread_labels[(channel_id, conversation_id)] = thread_id
        self._save()

    def consume_pending_first_thread_label(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> bool:
        key = (channel_id, conversation_id)
        if self._pending_first_thread_labels.get(key) != thread_id:
            return False
        self._pending_first_thread_labels.pop(key, None)
        self._save()
        return True

    def list_threads(self) -> list[ThreadRecord]:
        return sorted(list(self._threads.values()), key=lambda t: (t.created_seq, t.thread_id))

    def list_threads_for_cwd(self, cwd: str) -> list[ThreadRecord]:
        normalized = _normalize_cwd(cwd)
        return sorted(
            [
                thread
                for thread in self._threads.values()
                if _normalize_cwd(thread.cwd) == normalized
            ],
            key=lambda t: (t.created_seq, t.thread_id),
        )

    def get_binding(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        key = (channel_id, conversation_id)
        if key not in self._bindings:
            self._bindings[key] = ConversationBinding(
                channel_id=channel_id,
                conversation_id=conversation_id,
                permission_profile=self.default_permission_profile,
            )
            self._save()
        return self._bindings[key]

    def set_active_thread(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> ConversationBinding:
        thread = self.get_thread(thread_id)
        binding = self.get_binding(channel_id, conversation_id)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=thread_id)
        binding.selected_cwd = thread.cwd
        binding.active_thread_id = thread_id
        active_turn = self._thread_active_turns.get(thread_id)
        if active_turn is None:
            binding.active_turn_id = None
            binding.active_turn_status = None
        else:
            binding.active_turn_id, binding.active_turn_status = active_turn
        binding.last_seen_thread_name = thread.name or self.thread_label(thread_id)
        binding.last_seen_thread_path = thread.path or thread.cwd
        binding.last_seen_thread_status = thread.status
        self._save()
        return binding

    def set_active_turn(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> ConversationBinding:
        binding = self.set_active_thread(channel_id, conversation_id, thread_id)
        thread = self.get_thread(thread_id)
        self._thread_active_turns[thread_id] = (turn_id, status)
        self._mark_turn_superseded(thread, turn_id)
        thread.last_turn_id = turn_id
        thread.last_turn_status = status
        binding.active_turn_id = turn_id
        binding.active_turn_status = status
        self._save()
        return binding

    def note_inbound_message(
        self,
        channel_id: str,
        conversation_id: str,
        message_id: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.last_inbound_message_id = message_id
        self._save()
        return binding

    def clear_active_thread(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        clear_thread_turn: bool = False,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        if clear_thread_turn and binding.active_thread_id is not None:
            self._thread_active_turns.pop(binding.active_thread_id, None)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=None)
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        self._save()
        return binding

    def clear_active_turn(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        clear_thread_turn: bool = True,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        if clear_thread_turn and binding.active_thread_id is not None:
            self._thread_active_turns.pop(binding.active_thread_id, None)
        binding.active_turn_id = None
        binding.active_turn_status = None
        self._save()
        return binding

    def clear_stale_active_turns(self) -> int:
        cleared = 0
        for binding in self._bindings.values():
            if binding.active_turn_id is None and binding.active_turn_status is None:
                continue
            binding.active_turn_id = None
            binding.active_turn_status = None
            cleared += 1
        if self._thread_active_turns:
            self._thread_active_turns.clear()
            cleared = max(cleared, 1)
        if cleared:
            self._save()
        return cleared

    def note_turn_started(
        self,
        thread_id: str,
        *,
        turn_id: str,
        status: str,
    ) -> ConversationBinding | None:
        try:
            thread = self.get_thread(thread_id)
        except KeyError:
            return None
        thread.status = status
        self._mark_turn_superseded(thread, turn_id)
        thread.last_turn_id = turn_id
        thread.last_turn_status = status
        self._thread_active_turns[thread_id] = (turn_id, status)
        binding = self.find_binding_for_thread(thread_id)
        if binding is None:
            self._save()
            return None
        if binding.active_thread_id != thread_id:
            self._save()
            return binding
        binding.active_turn_id = turn_id
        binding.active_turn_status = status
        binding.last_seen_thread_status = status
        self._save()
        return binding

    def note_turn_completed(
        self,
        thread_id: str,
        *,
        turn_id: str,
        status: str,
    ) -> ConversationBinding | None:
        try:
            thread = self.get_thread(thread_id)
        except KeyError:
            return None
        thread.status = status
        binding = self.find_binding_for_thread(thread_id)
        stored_turn = self._thread_active_turns.get(thread_id)
        should_update_last_turn = (
            thread.last_turn_id is None
            or thread.last_turn_id == turn_id
            or (stored_turn is not None and stored_turn[0] == turn_id)
            or (binding is not None and binding.active_turn_id == turn_id)
        )
        if should_update_last_turn:
            thread.last_turn_id = turn_id
            thread.last_turn_status = status
        if stored_turn is not None and stored_turn[0] == turn_id:
            self._thread_active_turns.pop(thread_id, None)
        if binding is None:
            self._save()
            return None
        if binding.active_thread_id != thread_id:
            self._save()
            return binding
        if binding.active_turn_id == turn_id:
            binding.active_turn_id = None
            binding.active_turn_status = status
            binding.last_seen_thread_status = status
        elif binding.active_turn_id is None:
            binding.active_turn_status = status
            binding.last_seen_thread_status = status
        self._save()
        return binding

    def next_ticket_id(self, channel_id: str, conversation_id: str) -> str:
        binding = self.get_binding(channel_id, conversation_id)
        ticket_id = str(binding.next_ticket)
        binding.next_ticket += 1
        self._save()
        return ticket_id

    def set_permission_profile(
        self,
        channel_id: str,
        conversation_id: str,
        profile: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.permission_profile = profile
        self._save()
        return binding

    def set_model_override(
        self,
        channel_id: str,
        conversation_id: str,
        model: str | None,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.selected_model = model
        self._save()
        return binding

    def set_visibility_profile(
        self,
        channel_id: str,
        conversation_id: str,
        profile: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.visibility_profile = profile
        if profile == "minimal":
            binding.show_commentary = False
            binding.show_toolcalls = False
        elif profile == "verbose":
            binding.show_commentary = True
            binding.show_toolcalls = True
        else:
            binding.show_commentary = True
            binding.show_toolcalls = False
        self._save()
        return binding

    def set_commentary_visibility(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        enabled: bool,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.show_commentary = enabled
        self._save()
        return binding

    def set_toolcall_visibility(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        enabled: bool,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.show_toolcalls = enabled
        self._save()
        return binding

    def create_pending_request(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        request_method: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
    ) -> PendingRequest:
        request = PendingRequest(
            ticket_id=ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
            kind=kind,
            summary=summary,
            payload=payload,
            created_at=self.clock(),
            request_id=request_id,
            request_method=request_method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            status="pending",
        )
        self._pending_requests[
            self._pending_request_key(channel_id, conversation_id, ticket_id)
        ] = request
        binding = self.get_binding(channel_id, conversation_id)
        if ticket_id not in binding.pending_request_ids:
            binding.pending_request_ids.append(ticket_id)
        self._save()
        return request

    def get_pending_request(
        self,
        ticket_id: str,
        *,
        channel_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PendingRequest | None:
        if channel_id is not None and conversation_id is not None:
            return self._pending_requests.get(
                self._pending_request_key(channel_id, conversation_id, ticket_id)
            )
        matches = [
            request
            for request in self._pending_requests.values()
            if request.ticket_id == ticket_id
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def get_pending_request_by_request_id(self, request_id: str) -> PendingRequest | None:
        for request in self._pending_requests.values():
            if request.request_id == request_id:
                return request
        return None

    def list_pending_requests(
        self,
        channel_id: str,
        conversation_id: str,
    ) -> list[PendingRequest]:
        binding = self.get_binding(channel_id, conversation_id)
        requests = [
            self._pending_requests[
                self._pending_request_key(channel_id, conversation_id, ticket_id)
            ]
            for ticket_id in binding.pending_request_ids
            if self._pending_request_key(channel_id, conversation_id, ticket_id) in self._pending_requests
        ]
        return sorted(requests, key=lambda request: (request.created_at, request.ticket_id))

    def mark_pending_request_submitted(
        self,
        ticket_id: str,
        resolution: dict[str, Any],
        *,
        channel_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PendingRequest | None:
        request = self.get_pending_request(
            ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if request is None:
            return None
        request.submitted_at = self.clock()
        request.submitted_resolution = resolution
        request.status = "submitted"
        self._save()
        return request

    def resolve_pending_request(
        self,
        ticket_id: str,
        resolution: dict[str, Any],
        *,
        channel_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PendingRequest | None:
        request = self.get_pending_request(
            ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if request is None:
            return None
        request.status = "resolved"
        request.resolved_at = self.clock()
        request.resolution = resolution
        binding = self.get_binding(request.channel_id, request.conversation_id)
        if ticket_id in binding.pending_request_ids:
            binding.pending_request_ids.remove(ticket_id)
        self._pending_requests.pop(
            self._pending_request_key(request.channel_id, request.conversation_id, ticket_id),
            None,
        )
        self._save()
        return request

    def clear_pending_requests_for_turn(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        turn_id: str,
    ) -> int:
        target_keys: list[str] = []
        for ticket_id, request in self._pending_requests.items():
            if request.channel_id != channel_id or request.conversation_id != conversation_id:
                continue
            if request.thread_id != thread_id or request.turn_id != turn_id:
                continue
            target_keys.append(ticket_id)
        if not target_keys:
            return 0
        binding = self.get_binding(channel_id, conversation_id)
        for request_key in target_keys:
            request = self._pending_requests.get(request_key)
            if request is None:
                continue
            self._pending_requests.pop(request_key, None)
            if request.ticket_id in binding.pending_request_ids:
                binding.pending_request_ids.remove(request.ticket_id)
        self._save()
        return len(target_keys)

    def find_binding_for_thread(self, thread_id: str):
        for binding in self._bindings.values():
            if thread_id == binding.active_thread_id:
                return binding
        return None

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "threads": [self._serialize_thread(thread) for thread in self._threads.values()],
            "thread_active_turns": [
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "status": status,
                }
                for thread_id, (turn_id, status) in self._thread_active_turns.items()
            ],
            "thread_first_user_messages": self._thread_first_user_messages,
            "pending_first_thread_labels": [
                {
                    "channel_id": channel_id,
                    "conversation_id": conversation_id,
                    "thread_id": thread_id,
                }
                for (channel_id, conversation_id), thread_id in self._pending_first_thread_labels.items()
            ],
            "bindings": [self._serialize_binding(binding) for binding in self._bindings.values()],
            "pending_requests": [self._serialize_pending_request(request) for request in self._pending_requests.values()],
            "thread_order": self._thread_order,
            "seq": self._seq,
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._threads = {
            item["thread_id"]: self._load_thread(item) for item in payload.get("threads", [])
        }
        self._thread_active_turns = {
            item["thread_id"]: (item["turn_id"], item["status"])
            for item in payload.get("thread_active_turns", [])
        }
        self._thread_first_user_messages = dict(payload.get("thread_first_user_messages", {}))
        self._pending_first_thread_labels = {
            (item["channel_id"], item["conversation_id"]): item["thread_id"]
            for item in payload.get("pending_first_thread_labels", [])
        }
        self._thread_order = list(payload.get("thread_order", []))
        self._bindings = {
            (item["channel_id"], item["conversation_id"]): self._load_binding(item)
            for item in payload.get("bindings", [])
        }
        self._pending_requests = {
            self._pending_request_key(item["channel_id"], item["conversation_id"], item["ticket_id"]): PendingRequest(**item)
            for item in payload.get("pending_requests", [])
        }
        self._seq = int(payload.get("seq", 0))

    def _clear_pending_first_thread_label(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        next_thread_id: str | None,
    ) -> None:
        key = (channel_id, conversation_id)
        pending_thread_id = self._pending_first_thread_labels.get(key)
        if pending_thread_id is None or pending_thread_id == next_thread_id:
            return
        self._pending_first_thread_labels.pop(key, None)

    def _mark_turn_superseded(self, thread: ThreadRecord, next_turn_id: str) -> None:
        previous_turn_id = thread.last_turn_id
        if not previous_turn_id or previous_turn_id == next_turn_id:
            return
        if previous_turn_id in thread.stale_turn_ids:
            return
        thread.stale_turn_ids.append(previous_turn_id)
        if len(thread.stale_turn_ids) > 16:
            thread.stale_turn_ids = thread.stale_turn_ids[-16:]

    def _pending_request_key(
        self,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
    ) -> str:
        return f"{channel_id}:{conversation_id}:{ticket_id}"

    def _load_thread(self, item: dict[str, Any]) -> ThreadRecord:
        payload = dict(item)
        payload.pop("project_id", None)
        return ThreadRecord(**payload)

    def _load_binding(self, item: dict[str, Any]) -> ConversationBinding:
        payload = dict(item)
        payload.pop("active_project_id", None)
        return ConversationBinding(**payload)

    def _serialize_thread(self, thread: ThreadRecord) -> dict[str, Any]:
        return {
            "thread_id": thread.thread_id,
            "preview": thread.preview,
            "status": thread.status,
            "last_used_at": thread.last_used_at,
            "cwd": thread.cwd,
            "name": thread.name,
            "path": thread.path,
            "last_turn_id": thread.last_turn_id,
            "last_turn_status": thread.last_turn_status,
            "stale_turn_ids": list(thread.stale_turn_ids),
            "created_seq": thread.created_seq,
        }

    def _serialize_binding(self, binding: ConversationBinding) -> dict[str, Any]:
        return {
            "channel_id": binding.channel_id,
            "conversation_id": binding.conversation_id,
            "selected_cwd": binding.selected_cwd,
            "selected_model": binding.selected_model,
            "active_thread_id": binding.active_thread_id,
            "active_turn_id": binding.active_turn_id,
            "active_turn_status": binding.active_turn_status,
            "last_inbound_message_id": binding.last_inbound_message_id,
            "pending_request_ids": list(binding.pending_request_ids),
            "next_ticket": binding.next_ticket,
            "permission_profile": binding.permission_profile,
            "visibility_profile": binding.visibility_profile,
            "show_commentary": binding.show_commentary,
            "show_toolcalls": binding.show_toolcalls,
            "last_seen_thread_name": binding.last_seen_thread_name,
            "last_seen_thread_path": binding.last_seen_thread_path,
            "last_seen_thread_status": binding.last_seen_thread_status,
        }

    def _serialize_pending_request(self, request: PendingRequest) -> dict[str, Any]:
        return {
            "ticket_id": request.ticket_id,
            "channel_id": request.channel_id,
            "conversation_id": request.conversation_id,
            "kind": request.kind,
            "summary": request.summary,
            "payload": request.payload,
            "created_at": request.created_at,
            "request_id": request.request_id,
            "request_method": request.request_method,
            "thread_id": request.thread_id,
            "turn_id": request.turn_id,
            "item_id": request.item_id,
            "status": request.status,
            "submitted_at": request.submitted_at,
            "submitted_resolution": request.submitted_resolution,
            "resolved_at": request.resolved_at,
            "resolution": request.resolution,
        }

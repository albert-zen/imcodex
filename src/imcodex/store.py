from __future__ import annotations

import json
import os
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
        default_permission_profile: str = "autonomous",
    ):
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self.default_permission_profile = default_permission_profile
        self._threads: dict[str, ThreadRecord] = {}
        self._thread_active_turns: dict[str, tuple[str, str]] = {}
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
        merge_existing: bool = True,
    ) -> ThreadRecord:
        existing = self._threads.get(thread_id)
        thread = ThreadRecord(
            thread_id=thread_id,
            preview=preview if not merge_existing or existing is None else (preview or existing.preview),
            status=status if not merge_existing or existing is None else (status or existing.status),
            last_used_at=self.clock(),
            cwd=cwd if not merge_existing or existing is None else (cwd or existing.cwd),
            name=name if (not merge_existing or existing is None or name is not None) else existing.name,
            path=path if (not merge_existing or existing is None or path is not None) else existing.path,
            last_turn_id=existing.last_turn_id if existing is not None else None,
            last_turn_status=existing.last_turn_status if existing is not None else None,
            stale_turn_ids=list(existing.stale_turn_ids) if existing is not None else [],
            created_seq=existing.created_seq if existing is not None else self._next_seq(),
        )
        self._threads[thread_id] = thread
        if thread_id not in self._thread_order:
            self._thread_order.append(thread_id)
        return thread

    def set_selected_cwd(
        self,
        channel_id: str,
        conversation_id: str,
        cwd: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.selected_cwd = cwd
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        binding.last_seen_thread_name = None
        binding.last_seen_thread_path = None
        binding.last_seen_thread_status = None
        self._save()
        return binding

    def get_thread(self, thread_id: str) -> ThreadRecord:
        return self._threads[thread_id]

    def thread_label(self, thread_id: str) -> str:
        thread = self.get_thread(thread_id)
        if thread.name:
            label = _clip_thread_label(thread.name)
            if label:
                return label
        preview = _clip_thread_label(thread.preview)
        if preview:
            return preview
        return "Untitled thread"

    def note_thread_status(
        self,
        thread_id: str,
        *,
        status: str,
        channel_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ThreadRecord | None:
        thread = self._threads.get(thread_id)
        if thread is not None:
            thread.status = status
        if channel_id is not None and conversation_id is not None:
            binding = self.get_binding(channel_id, conversation_id)
            if binding.active_thread_id == thread_id:
                binding.last_seen_thread_status = status
        return thread

    def note_thread_name(self, thread_id: str, *, name: str) -> ThreadRecord | None:
        thread = self._threads.get(thread_id)
        if thread is not None:
            thread.name = name
        for binding in self._bindings.values():
            if binding.active_thread_id == thread_id:
                binding.last_seen_thread_name = name
        return thread

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
        return self._bindings[key]

    def set_active_thread(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        thread = self._threads.get(thread_id)
        binding.active_thread_id = thread_id
        binding.selected_cwd = thread.cwd if thread is not None and thread.cwd else None
        active_turn = self._thread_active_turns.get(thread_id)
        if active_turn is None:
            binding.active_turn_id = None
            binding.active_turn_status = None
        else:
            binding.active_turn_id, binding.active_turn_status = active_turn
        if thread is not None:
            binding.last_seen_thread_name = thread.name or self.thread_label(thread_id)
            binding.last_seen_thread_path = thread.path or thread.cwd
            binding.last_seen_thread_status = thread.status
        else:
            binding.last_seen_thread_name = None
            binding.last_seen_thread_path = None
            binding.last_seen_thread_status = None
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
        thread = self._threads.get(thread_id)
        self._thread_active_turns[thread_id] = (turn_id, status)
        if thread is not None:
            self._mark_turn_superseded(thread, turn_id)
            thread.last_turn_id = turn_id
            thread.last_turn_status = status
            thread.status = status
        binding.active_turn_id = turn_id
        binding.active_turn_status = status
        binding.last_seen_thread_status = status
        return binding

    def note_inbound_message(
        self,
        channel_id: str,
        conversation_id: str,
        message_id: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.last_inbound_message_id = message_id
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
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        binding.last_seen_thread_name = None
        binding.last_seen_thread_path = None
        binding.last_seen_thread_status = None
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
        return cleared

    def note_turn_started(
        self,
        thread_id: str,
        *,
        turn_id: str,
        status: str,
    ) -> ThreadRecord | None:
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        thread.status = status
        self._mark_turn_superseded(thread, turn_id)
        thread.last_turn_id = turn_id
        thread.last_turn_status = status
        self._thread_active_turns[thread_id] = (turn_id, status)
        return thread

    def note_turn_completed(
        self,
        thread_id: str,
        *,
        turn_id: str,
        status: str,
    ) -> ThreadRecord | None:
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        thread.status = status
        stored_turn = self._thread_active_turns.get(thread_id)
        should_update_last_turn = (
            thread.last_turn_id is None
            or thread.last_turn_id == turn_id
            or (stored_turn is not None and stored_turn[0] == turn_id)
        )
        if should_update_last_turn:
            thread.last_turn_id = turn_id
            thread.last_turn_status = status
        if stored_turn is not None and stored_turn[0] == turn_id:
            self._thread_active_turns.pop(thread_id, None)
        return thread

    def next_ticket_id(self, channel_id: str, conversation_id: str) -> str:
        binding = self.get_binding(channel_id, conversation_id)
        next_ticket = binding.next_ticket
        for ticket_id in binding.pending_request_ids:
            if ticket_id.isdigit():
                next_ticket = max(next_ticket, int(ticket_id) + 1)
        ticket_id = str(next_ticket)
        binding.next_ticket = next_ticket + 1
        return ticket_id

    def set_permission_profile(
        self,
        channel_id: str,
        conversation_id: str,
        profile: str,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.permission_profile = profile
        return binding

    def set_model_override(
        self,
        channel_id: str,
        conversation_id: str,
        model: str | None,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.selected_model = model
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
        if ticket_id.isdigit():
            binding.next_ticket = max(binding.next_ticket, int(ticket_id) + 1)
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
        for request_key, request in self._pending_requests.items():
            if request.channel_id != channel_id or request.conversation_id != conversation_id:
                continue
            if request.thread_id != thread_id or request.turn_id != turn_id:
                continue
            target_keys.append(request_key)
        if not target_keys:
            return 0
        binding = self.get_binding(channel_id, conversation_id)
        for request_key in target_keys:
            request = self._pending_requests.pop(request_key, None)
            if request is None:
                continue
            if request.ticket_id in binding.pending_request_ids:
                binding.pending_request_ids.remove(request.ticket_id)
        self._save()
        return len(target_keys)

    def iter_bindings(self) -> list[ConversationBinding]:
        return list(self._bindings.values())

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        bindings = [
            self._serialize_binding(binding)
            for binding in self._bindings.values()
            if binding.active_thread_id is not None or binding.selected_cwd is not None
        ]
        payload = {
            "bindings": bindings,
            "pending_requests": [
                self._serialize_pending_request(request)
                for request in self._pending_requests.values()
            ],
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._bindings = {}
        self._pending_requests = {}
        for item in payload.get("bindings", []):
            binding = self._load_binding(item)
            self._bindings[(binding.channel_id, binding.conversation_id)] = binding
        for item in payload.get("pending_requests", []):
            request = self._load_pending_request(item)
            key = self._pending_request_key(
                request.channel_id,
                request.conversation_id,
                request.ticket_id,
            )
            self._pending_requests[key] = request
            binding = self.get_binding(request.channel_id, request.conversation_id)
            if request.ticket_id not in binding.pending_request_ids:
                binding.pending_request_ids.append(request.ticket_id)
            if request.ticket_id.isdigit():
                binding.next_ticket = max(binding.next_ticket, int(request.ticket_id) + 1)

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

    def _load_binding(self, item: dict[str, Any]) -> ConversationBinding:
        bootstrap_cwd = item.get("bootstrap_cwd", item.get("selected_cwd"))
        thread_id = item.get("thread_id", item.get("active_thread_id"))
        return ConversationBinding(
            channel_id=item["channel_id"],
            conversation_id=item["conversation_id"],
            thread_id=str(thread_id) if thread_id is not None else None,
            bootstrap_cwd=str(bootstrap_cwd) if bootstrap_cwd is not None else None,
            permission_profile=self.default_permission_profile,
        )

    def _load_pending_request(self, item: dict[str, Any]) -> PendingRequest:
        return PendingRequest(
            ticket_id=str(item["ticket_id"]),
            channel_id=item["channel_id"],
            conversation_id=item["conversation_id"],
            kind=str(item.get("kind") or "request"),
            summary=str(item.get("summary") or item.get("kind") or "Pending request"),
            payload=dict(item.get("payload") or {}),
            created_at=float(item.get("created_at", 0.0)),
            request_id=str(item["request_id"]) if item.get("request_id") is not None else None,
            request_method=item.get("request_method"),
            thread_id=str(item["thread_id"]) if item.get("thread_id") is not None else None,
            turn_id=str(item["turn_id"]) if item.get("turn_id") is not None else None,
            item_id=str(item["item_id"]) if item.get("item_id") is not None else None,
            status=str(item.get("status") or "pending"),
            submitted_at=float(item["submitted_at"]) if item.get("submitted_at") is not None else None,
            submitted_resolution=dict(item.get("submitted_resolution") or {}) or None,
            resolved_at=float(item["resolved_at"]) if item.get("resolved_at") is not None else None,
            resolution=dict(item.get("resolution") or {}) or None,
        )

    def _serialize_binding(self, binding: ConversationBinding) -> dict[str, Any]:
        return {
            "channel_id": binding.channel_id,
            "conversation_id": binding.conversation_id,
            "thread_id": binding.active_thread_id,
            "bootstrap_cwd": binding.selected_cwd,
        }

    def _serialize_pending_request(self, request: PendingRequest) -> dict[str, Any]:
        return {
            "channel_id": request.channel_id,
            "conversation_id": request.conversation_id,
            "ticket_id": request.ticket_id,
            "request_id": request.request_id,
            "thread_id": request.thread_id,
            "turn_id": request.turn_id,
            "kind": request.kind,
        }

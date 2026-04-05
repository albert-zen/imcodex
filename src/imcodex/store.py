from __future__ import annotations

import hashlib
import os
import json
import textwrap
from pathlib import Path
from typing import Any, Callable
from dataclasses import asdict

from .models import ConversationBinding, PendingRequest, ProjectRecord, ThreadRecord


Clock = Callable[[], float]


def _normalize_cwd(cwd: str) -> str:
    return os.path.normcase(os.path.normpath(cwd))


def _project_id_for_cwd(cwd: str) -> str:
    return hashlib.sha1(_normalize_cwd(cwd).encode("utf-8")).hexdigest()[:12]


def _display_name_for_cwd(cwd: str) -> str:
    return Path(_normalize_cwd(cwd)).name or _normalize_cwd(cwd)


def _clip_thread_label(text: str) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    shortened = textwrap.shorten(collapsed, width=60, placeholder="...")
    if shortened != "...":
        return shortened
    return f"{collapsed[:57].rstrip()}..."


class ConversationStore:
    def __init__(self, clock: Clock, state_path: str | Path | None = None):
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self._projects: dict[str, ProjectRecord] = {}
        self._projects_by_cwd: dict[str, str] = {}
        self._threads: dict[str, ThreadRecord] = {}
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

    def _touch_project(self, project: ProjectRecord) -> None:
        project.last_used_at = self.clock()

    def _ensure_project(self, cwd: str) -> ProjectRecord:
        normalized = _normalize_cwd(cwd)
        project_id = self._projects_by_cwd.get(normalized)
        if project_id is not None:
            project = self._projects[project_id]
            self._touch_project(project)
            return project
        project = ProjectRecord(
            project_id=_project_id_for_cwd(cwd),
            cwd=cwd,
            display_name=_display_name_for_cwd(cwd),
            last_used_at=self.clock(),
            created_seq=self._next_seq(),
        )
        self._projects[project.project_id] = project
        self._projects_by_cwd[normalized] = project.project_id
        return project

    def record_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        preview: str,
        status: str = "idle",
    ) -> ThreadRecord:
        project = self._ensure_project(cwd)
        existing = self._threads.get(thread_id)
        thread = ThreadRecord(
            thread_id=thread_id,
            project_id=project.project_id,
            preview=preview or (existing.preview if existing is not None else ""),
            status=status,
            last_used_at=self.clock(),
            cwd=cwd,
            created_seq=existing.created_seq if existing is not None else self._next_seq(),
        )
        self._threads[thread_id] = thread
        if thread_id not in self._thread_order:
            self._thread_order.append(thread_id)
        self._touch_project(project)
        self._save()
        return thread

    def ensure_project(self, cwd: str) -> ProjectRecord:
        project = self._ensure_project(cwd)
        self._save()
        return project

    def get_thread(self, thread_id: str) -> ThreadRecord:
        return self._threads[thread_id]

    def get_project(self, project_id: str) -> ProjectRecord:
        return self._projects[project_id]

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
        preview = _clip_thread_label(thread.preview)
        if preview:
            return preview
        first_user_message = self._thread_first_user_messages.get(thread_id, "")
        if first_user_message:
            return first_user_message
        return "Untitled thread"

    def mark_pending_first_thread_label(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> None:
        self.get_thread(thread_id)
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

    def list_projects(self) -> list[ProjectRecord]:
        return sorted(
            self._projects.values(),
            key=lambda p: (-p.last_used_at, p.created_seq, p.display_name),
        )

    def list_threads(self, project_id: str | None = None) -> list[ThreadRecord]:
        threads = self._threads.values()
        if project_id is not None:
            threads = [thread for thread in threads if thread.project_id == project_id]
        return sorted(
            threads,
            key=lambda t: (t.created_seq, t.thread_id),
        )

    def get_binding(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        key = (channel_id, conversation_id)
        if key not in self._bindings:
            self._bindings[key] = ConversationBinding(
                channel_id=channel_id,
                conversation_id=conversation_id,
            )
            self._save()
        return self._bindings[key]

    def set_active_project(
        self,
        channel_id: str,
        conversation_id: str,
        project_id: str,
    ) -> ConversationBinding:
        self.get_project(project_id)
        binding = self.get_binding(channel_id, conversation_id)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=None)
        binding.active_project_id = project_id
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        self._save()
        return binding

    def set_active_thread(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> ConversationBinding:
        thread = self.get_thread(thread_id)
        binding = self.get_binding(channel_id, conversation_id)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=thread_id)
        binding.active_project_id = thread.project_id
        binding.active_thread_id = thread_id
        if thread_id not in binding.known_thread_ids:
            binding.known_thread_ids.append(thread_id)
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

    def clear_active_thread(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=None)
        binding.active_thread_id = None
        binding.active_turn_id = None
        binding.active_turn_status = None
        self._save()
        return binding

    def clear_active_turn(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
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
        binding = self.find_binding_for_thread(thread_id)
        if binding is None:
            return None
        self._clear_pending_first_thread_label(binding.channel_id, binding.conversation_id, next_thread_id=thread_id)
        binding.active_thread_id = thread_id
        if thread_id not in binding.known_thread_ids:
            binding.known_thread_ids.append(thread_id)
        binding.active_turn_id = turn_id
        binding.active_turn_status = status
        self._save()
        return binding

    def note_turn_completed(
        self,
        thread_id: str,
        *,
        turn_id: str,
        status: str,
    ) -> ConversationBinding | None:
        binding = self.find_binding_for_thread(thread_id)
        if binding is None:
            return None
        if binding.active_turn_id == turn_id:
            binding.active_turn_id = None
        binding.active_turn_status = status
        self._save()
        return binding

    def next_ticket_id(self, channel_id: str, conversation_id: str) -> str:
        binding = self.get_binding(channel_id, conversation_id)
        ticket_id = str(binding.next_ticket)
        binding.next_ticket += 1
        self._save()
        return ticket_id

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
        )
        self._pending_requests[ticket_id] = request
        binding = self.get_binding(channel_id, conversation_id)
        if ticket_id not in binding.pending_request_ids:
            binding.pending_request_ids.append(ticket_id)
        self._save()
        return request

    def get_pending_request(self, ticket_id: str) -> PendingRequest | None:
        return self._pending_requests.get(ticket_id)

    def resolve_pending_request(
        self,
        ticket_id: str,
        resolution: dict[str, Any],
    ) -> PendingRequest | None:
        request = self._pending_requests.get(ticket_id)
        if request is None:
            return None
        request.resolved_at = self.clock()
        request.resolution = resolution
        binding = self.get_binding(request.channel_id, request.conversation_id)
        if ticket_id in binding.pending_request_ids:
            binding.pending_request_ids.remove(ticket_id)
        self._pending_requests.pop(ticket_id, None)
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
        for ticket_id in target_keys:
            self._pending_requests.pop(ticket_id, None)
            if ticket_id in binding.pending_request_ids:
                binding.pending_request_ids.remove(ticket_id)
        self._save()
        return len(target_keys)

    def active_projects_for_conversation(self, channel_id: str, conversation_id: str) -> list[ProjectRecord]:
        binding = self.get_binding(channel_id, conversation_id)
        if binding.active_project_id:
            return [self.get_project(binding.active_project_id)]
        return self.list_projects()

    def find_binding_for_thread(self, thread_id: str):
        for binding in self._bindings.values():
            if thread_id == binding.active_thread_id or thread_id in binding.known_thread_ids:
                return binding
        return None

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": [asdict(project) for project in self._projects.values()],
            "threads": [asdict(thread) for thread in self._threads.values()],
            "thread_first_user_messages": self._thread_first_user_messages,
            "pending_first_thread_labels": [
                {
                    "channel_id": channel_id,
                    "conversation_id": conversation_id,
                    "thread_id": thread_id,
                }
                for (channel_id, conversation_id), thread_id in self._pending_first_thread_labels.items()
            ],
            "bindings": [asdict(binding) for binding in self._bindings.values()],
            "pending_requests": [asdict(request) for request in self._pending_requests.values()],
            "thread_order": self._thread_order,
            "seq": self._seq,
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._projects = {
            item["project_id"]: ProjectRecord(**item) for item in payload.get("projects", [])
        }
        self._projects_by_cwd = {
            _normalize_cwd(project.cwd): project.project_id for project in self._projects.values()
        }
        self._threads = {
            item["thread_id"]: ThreadRecord(**item) for item in payload.get("threads", [])
        }
        self._thread_first_user_messages = dict(payload.get("thread_first_user_messages", {}))
        self._pending_first_thread_labels = {
            (item["channel_id"], item["conversation_id"]): item["thread_id"]
            for item in payload.get("pending_first_thread_labels", [])
        }
        self._thread_order = list(payload.get("thread_order", []))
        self._bindings = {
            (item["channel_id"], item["conversation_id"]): ConversationBinding(**item)
            for item in payload.get("bindings", [])
        }
        self._pending_requests = {
            item["ticket_id"]: PendingRequest(**item) for item in payload.get("pending_requests", [])
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

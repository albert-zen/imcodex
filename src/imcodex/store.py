from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .models import ConversationBinding, NativeThreadSnapshot, PendingNativeRequestRoute


Clock = Callable[[], float]


class ConversationStore:
    def __init__(
        self,
        clock: Clock,
        state_path: str | Path | None = None,
    ) -> None:
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self._bindings: dict[tuple[str, str], ConversationBinding] = {}
        self._pending_requests: dict[str, PendingNativeRequestRoute] = {}
        self._thread_snapshots: dict[str, NativeThreadSnapshot] = {}
        self._active_turns: dict[str, tuple[str, str]] = {}
        self._suppressed_turns: set[tuple[str, str]] = set()
        self._next_model_overrides: dict[tuple[str, str], str | None] = {}
        if self.state_path and self.state_path.exists():
            self._load()

    def get_binding(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        key = (channel_id, conversation_id)
        if key not in self._bindings:
            self._bindings[key] = ConversationBinding(
                channel_id=channel_id,
                conversation_id=conversation_id,
            )
        return self._bindings[key]

    def iter_bindings(self) -> list[ConversationBinding]:
        return list(self._bindings.values())

    def find_binding_by_thread_id(self, thread_id: str) -> ConversationBinding | None:
        for binding in self._bindings.values():
            if binding.thread_id == thread_id:
                return binding
        return None

    def set_bootstrap_cwd(self, channel_id: str, conversation_id: str, cwd: str) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.bootstrap_cwd = cwd
        binding.thread_id = None
        self._next_model_overrides.pop((channel_id, conversation_id), None)
        self._save()
        return binding

    def bind_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> ConversationBinding:
        for key, existing in self._bindings.items():
            if key == (channel_id, conversation_id):
                continue
            if existing.thread_id == thread_id:
                existing.thread_id = None
        for route in self._pending_requests.values():
            if route.thread_id == thread_id:
                route.channel_id = channel_id
                route.conversation_id = conversation_id
        binding = self.get_binding(channel_id, conversation_id)
        binding.thread_id = thread_id
        self._save()
        return binding

    def bind_thread_with_cwd(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        cwd: str | None,
    ) -> ConversationBinding:
        binding = self.bind_thread(channel_id, conversation_id, thread_id)
        if cwd:
            binding.bootstrap_cwd = cwd
            self._save()
        return binding

    def clear_thread_binding(self, channel_id: str, conversation_id: str) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        if binding.thread_id is not None:
            self._active_turns.pop(binding.thread_id, None)
            self._suppressed_turns = {
                key for key in self._suppressed_turns if key[0] != binding.thread_id
            }
        binding.thread_id = None
        self._save()
        return binding

    def note_thread_snapshot(self, snapshot: NativeThreadSnapshot) -> NativeThreadSnapshot:
        self._thread_snapshots[snapshot.thread_id] = snapshot
        return snapshot

    def update_thread_snapshot(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        preview: str | None = None,
        status: str | None = None,
        name: str | None = None,
        path: str | None = None,
    ) -> None:
        snapshot = self._thread_snapshots.get(thread_id)
        if snapshot is None:
            return
        if cwd is not None:
            snapshot.cwd = cwd
        if preview is not None:
            snapshot.preview = preview
        if status is not None:
            snapshot.status = status
        if name is not None:
            snapshot.name = name
        if path is not None:
            snapshot.path = path

    def get_thread_snapshot(self, thread_id: str) -> NativeThreadSnapshot | None:
        return self._thread_snapshots.get(thread_id)

    def current_cwd(self, channel_id: str, conversation_id: str) -> str | None:
        binding = self.get_binding(channel_id, conversation_id)
        if binding.thread_id:
            snapshot = self.get_thread_snapshot(binding.thread_id)
            if snapshot and snapshot.cwd:
                return snapshot.cwd
        return binding.bootstrap_cwd

    def note_active_turn(self, thread_id: str, turn_id: str, status: str) -> None:
        self._active_turns[thread_id] = (turn_id, status)
        self._suppressed_turns.discard((thread_id, turn_id))

    def complete_turn(self, thread_id: str, turn_id: str, status: str) -> None:
        active = self._active_turns.get(thread_id)
        if active is not None and active[0] == turn_id:
            self._active_turns.pop(thread_id, None)
        self._suppressed_turns.discard((thread_id, turn_id))
        snapshot = self._thread_snapshots.get(thread_id)
        if snapshot is not None:
            snapshot.status = status

    def clear_active_turn(self, thread_id: str) -> None:
        self._active_turns.pop(thread_id, None)

    def get_active_turn(self, thread_id: str) -> tuple[str, str] | None:
        return self._active_turns.get(thread_id)

    def suppress_turn(self, thread_id: str, turn_id: str) -> None:
        self._suppressed_turns.add((thread_id, turn_id))

    def is_turn_suppressed(self, thread_id: str, turn_id: str) -> bool:
        return (thread_id, turn_id) in self._suppressed_turns

    def set_next_model_override(
        self,
        channel_id: str,
        conversation_id: str,
        model: str | None,
    ) -> None:
        key = (channel_id, conversation_id)
        if model is None:
            self._next_model_overrides.pop(key, None)
            return
        self._next_model_overrides[key] = model

    def pop_next_model_override(self, channel_id: str, conversation_id: str) -> str | None:
        return self._next_model_overrides.pop((channel_id, conversation_id), None)

    def set_visibility_profile(self, channel_id: str, conversation_id: str, profile: str) -> ConversationBinding:
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

    def note_inbound_message(self, channel_id: str, conversation_id: str, message_id: str) -> None:
        binding = self.get_binding(channel_id, conversation_id)
        binding.reply_context["last_inbound_message_id"] = message_id
        self._save()

    def upsert_pending_request(
        self,
        *,
        request_id: str,
        request_handle: str | None,
        channel_id: str,
        conversation_id: str,
        thread_id: str | None,
        turn_id: str | None,
        kind: str,
        request_method: str | None,
        payload: dict | None = None,
    ) -> PendingNativeRequestRoute:
        route = PendingNativeRequestRoute(
            request_id=request_id,
            request_handle=request_handle or request_id[:8],
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=kind,
            request_method=request_method,
            payload=dict(payload or {}),
        )
        self._pending_requests[request_id] = route
        self._save()
        return route

    def list_pending_requests(self, channel_id: str, conversation_id: str) -> list[PendingNativeRequestRoute]:
        return [
            route
            for route in self._pending_requests.values()
            if route.channel_id == channel_id and route.conversation_id == conversation_id
        ]

    def match_pending_request(
        self,
        channel_id: str,
        conversation_id: str,
        token: str | None = None,
        *,
        kind: str | None = None,
    ) -> PendingNativeRequestRoute | None:
        candidates = self.list_pending_requests(channel_id, conversation_id)
        if kind is not None:
            candidates = [route for route in candidates if route.kind == kind]
        if not candidates:
            return None
        if token is None or not token.strip():
            if len(candidates) == 1:
                return candidates[0]
            return None
        token = token.strip()
        for route in candidates:
            if token == route.request_id or token == route.request_handle:
                return route
        prefix_matches = [
            route
            for route in candidates
            if route.request_id.startswith(token)
            or (route.request_handle is not None and route.request_handle.startswith(token))
        ]
        if len(prefix_matches) > 1:
            raise ValueError(f"Ambiguous request id prefix: {token}")
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        return None

    def remove_pending_request(self, request_id: str) -> PendingNativeRequestRoute | None:
        route = self._pending_requests.pop(request_id, None)
        if route is not None:
            self._save()
        return route

    def remove_pending_requests_for_turn(self, thread_id: str, turn_id: str) -> None:
        removed = [
            request_id
            for request_id, route in self._pending_requests.items()
            if route.thread_id == thread_id and route.turn_id == turn_id
        ]
        if not removed:
            return
        for request_id in removed:
            self._pending_requests.pop(request_id, None)
        self._save()

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "bindings": [
                {
                    "channel_id": binding.channel_id,
                    "conversation_id": binding.conversation_id,
                    "thread_id": binding.thread_id,
                    "bootstrap_cwd": binding.bootstrap_cwd,
                    "visibility_profile": binding.visibility_profile,
                    "show_commentary": binding.show_commentary,
                    "show_toolcalls": binding.show_toolcalls,
                    "reply_context": binding.reply_context,
                }
                for binding in self._bindings.values()
                if binding.thread_id is not None
                or binding.bootstrap_cwd is not None
                or binding.visibility_profile != "standard"
                or binding.show_commentary is not True
                or binding.show_toolcalls is not False
                or binding.reply_context
            ],
            "pending_requests": [
                {
                    "request_id": route.request_id,
                    "request_handle": route.request_handle,
                    "channel_id": route.channel_id,
                    "conversation_id": route.conversation_id,
                    "thread_id": route.thread_id,
                    "turn_id": route.turn_id,
                    "kind": route.kind,
                    "request_method": route.request_method,
                    "payload": route.payload,
                }
                for route in self._pending_requests.values()
            ],
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != 2:
            return
        for item in payload.get("bindings", []):
            if "channel_id" not in item or "conversation_id" not in item:
                continue
            binding = ConversationBinding(
                channel_id=str(item["channel_id"]),
                conversation_id=str(item["conversation_id"]),
                thread_id=str(item["thread_id"]) if item.get("thread_id") is not None else None,
                bootstrap_cwd=str(item["bootstrap_cwd"]) if item.get("bootstrap_cwd") is not None else None,
                visibility_profile=str(item.get("visibility_profile") or "standard"),
                show_commentary=bool(item.get("show_commentary", True)),
                show_toolcalls=bool(item.get("show_toolcalls", False)),
                reply_context=dict(item.get("reply_context") or {}),
            )
            self._bindings[(binding.channel_id, binding.conversation_id)] = binding
        for item in payload.get("pending_requests", []):
            if "request_id" not in item or "channel_id" not in item or "conversation_id" not in item:
                continue
            route = PendingNativeRequestRoute(
                request_id=str(item["request_id"]),
                request_handle=str(item["request_handle"]) if item.get("request_handle") is not None else None,
                channel_id=str(item["channel_id"]),
                conversation_id=str(item["conversation_id"]),
                thread_id=str(item["thread_id"]) if item.get("thread_id") is not None else None,
                turn_id=str(item["turn_id"]) if item.get("turn_id") is not None else None,
                kind=str(item.get("kind") or "request"),
                request_method=str(item["request_method"]) if item.get("request_method") is not None else None,
                payload=dict(item.get("payload") or {}),
            )
            self._pending_requests[route.request_id] = route

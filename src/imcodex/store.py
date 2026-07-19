from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from collections import deque
from pathlib import Path
from threading import Lock, RLock
import tempfile
from typing import Callable

from .models import (
    ConversationBinding,
    NativeAppServerJournalEntry,
    NativeThreadSnapshot,
    PendingNativeRequestRoute,
    PendingTerminalDelivery,
    ThreadBrowserContext,
)
from .store_native_events import (
    DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT as _DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT,
    NativeEventJournalMixin,
)
from .store_pending_requests import PendingRequestStoreMixin
from .store_terminal_deliveries import TerminalDeliveryStoreMixin


Clock = Callable[[], float]
logger = logging.getLogger(__name__)


class ConversationStore(
    NativeEventJournalMixin,
    PendingRequestStoreMixin,
    TerminalDeliveryStoreMixin,
):
    INBOUND_DEDUP_WINDOW_S = 2.0
    RECENT_INBOUND_MESSAGE_ID_LIMIT = 1024
    RECENT_INBOUND_RESPONSE_LIMIT = 32
    DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT = _DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT

    def __init__(
        self,
        clock: Clock,
        state_path: str | Path | None = None,
        native_event_journal_limit: int = _DEFAULT_NATIVE_EVENT_JOURNAL_LIMIT,
    ) -> None:
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        journal_limit = max(1, int(native_event_journal_limit))
        self._bindings: dict[tuple[str, str], ConversationBinding] = {}
        self._pending_requests: dict[str, PendingNativeRequestRoute] = {}
        self._pending_terminal_deliveries: dict[tuple[str, str], PendingTerminalDelivery] = {}
        self._thread_snapshots: dict[str, NativeThreadSnapshot] = {}
        self._thread_browser_contexts: dict[tuple[str, str], ThreadBrowserContext] = {}
        self._active_turns: dict[str, tuple[str, str]] = {}
        self._suppressed_turns: set[tuple[str, str]] = set()
        self._native_appserver_journal: deque[NativeAppServerJournalEntry] = deque(maxlen=journal_limit)
        self._native_appserver_journal_sequence = 0
        self._recent_inbound_fingerprints: dict[tuple[str, str], dict[str, float]] = {}
        self._save_lock = RLock()
        self._revision_lock = Lock()
        self._next_state_revision = 0
        self._persisted_state_revision = 0
        self._async_persistence_lock = asyncio.Lock()
        self._dirty_inbound_commits: set[tuple[str, str, str]] = set()
        self._queued_state_write: tuple[int, str] | None = None
        self._background_writer_task: asyncio.Task[None] | None = None
        self._background_write_failures: dict[int, BaseException] = {}
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
        previous_thread_id = binding.thread_id
        binding.thread_id = thread_id
        if previous_thread_id and previous_thread_id != thread_id:
            self._remove_terminal_deliveries_for_thread(
                previous_thread_id,
                preserve_staged=True,
            )
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
            self._remove_terminal_deliveries_for_thread(
                binding.thread_id,
                preserve_staged=True,
            )
            self._active_turns.pop(binding.thread_id, None)
            self._suppressed_turns = {key for key in self._suppressed_turns if key[0] != binding.thread_id}
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

    def set_thread_browser_context(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        thread_ids: list[str],
        page: int,
        total: int,
        query: str | None,
        all_thread_ids: list[str] | None = None,
        project_paths: list[str] | None = None,
        project_path: str | None = None,
        ttl_s: float = 900.0,
    ) -> ThreadBrowserContext:
        context = ThreadBrowserContext(
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_ids=list(thread_ids),
            page=page,
            total=total,
            query=query,
            all_thread_ids=list(all_thread_ids or thread_ids),
            project_paths=list(project_paths or []),
            project_path=project_path,
            expires_at=self.clock() + ttl_s,
        )
        self._thread_browser_contexts[(channel_id, conversation_id)] = context
        return context

    def get_thread_browser_context(
        self,
        channel_id: str,
        conversation_id: str,
    ) -> ThreadBrowserContext | None:
        key = (channel_id, conversation_id)
        context = self._thread_browser_contexts.get(key)
        if context is None:
            return None
        if context.expires_at <= self.clock():
            self._thread_browser_contexts.pop(key, None)
            return None
        return context

    def clear_thread_browser_context(self, channel_id: str, conversation_id: str) -> None:
        self._thread_browser_contexts.pop((channel_id, conversation_id), None)

    def note_active_turn(self, thread_id: str, turn_id: str, status: str) -> None:
        self._active_turns[thread_id] = (turn_id, status)
        self._suppressed_turns.discard((thread_id, turn_id))
        self.watch_terminal_delivery(thread_id, turn_id)

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

    def set_visibility_profile(self, channel_id: str, conversation_id: str, profile: str) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.visibility_profile = profile
        if profile == "minimal":
            binding.show_commentary = False
            binding.show_toolcalls = False
            binding.show_system = False
        elif profile == "verbose":
            binding.show_commentary = True
            binding.show_toolcalls = True
            binding.show_system = True
        else:
            binding.show_commentary = True
            binding.show_toolcalls = False
            binding.show_system = False
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

    def set_system_visibility(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        enabled: bool,
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.show_system = enabled
        self._save()
        return binding

    def note_inbound_message(
        self,
        channel_id: str,
        conversation_id: str,
        message_id: str,
        *,
        user_id: str | None = None,
    ) -> None:
        """Update in-memory reply/routing context before handling."""

        binding = self.get_binding(channel_id, conversation_id)
        binding.reply_context["last_inbound_message_id"] = message_id
        binding.reply_context["last_inbound_seen_at"] = self.clock()
        if user_id:
            binding.reply_context["last_inbound_user_id"] = user_id

    def mark_inbound_message_processed(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str,
        text_fingerprint: str,
        response_payload: list[dict] | None = None,
    ) -> None:
        self._record_inbound_message_processed(
            channel_id=channel_id,
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message_id,
            text_fingerprint=text_fingerprint,
            response_payload=response_payload,
        )
        self._save()

    async def commit_inbound_message_processed(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str,
        text_fingerprint: str,
        response_payload: list[dict] | None = None,
    ) -> None:
        async with self._async_persistence_lock:
            self._record_inbound_message_processed(
                channel_id=channel_id,
                conversation_id=conversation_id,
                user_id=user_id,
                message_id=message_id,
                text_fingerprint=text_fingerprint,
                response_payload=response_payload,
            )
            if self.state_path is None or not message_id:
                return
            commit_key = (channel_id, conversation_id, message_id)
            self._dirty_inbound_commits.add(commit_key)
            try:
                revision, serialized = self._snapshot_state()
                await self._write_state_async(serialized, revision)
            except asyncio.CancelledError:
                # _write_state_async only re-raises cancellation after the
                # shielded write has completed successfully.
                self._dirty_inbound_commits.discard(commit_key)
                raise
            except BaseException:
                # Keep the processed marker and cached response in memory.
                # A platform retry can persist and replay them without
                # executing the native command a second time.
                raise
            else:
                self._dirty_inbound_commits.discard(commit_key)

    async def ensure_inbound_message_durable(
        self,
        channel_id: str,
        conversation_id: str,
        message_id: str,
    ) -> None:
        commit_key = (channel_id, conversation_id, message_id)
        if commit_key not in self._dirty_inbound_commits:
            return
        async with self._async_persistence_lock:
            if commit_key not in self._dirty_inbound_commits:
                return
            try:
                revision, serialized = self._snapshot_state()
                await self._write_state_async(serialized, revision)
            except asyncio.CancelledError:
                self._dirty_inbound_commits.discard(commit_key)
                raise
            except BaseException:
                raise
            else:
                self._dirty_inbound_commits.discard(commit_key)

    async def _write_state_async(self, serialized: str, revision: int) -> None:
        write_task = asyncio.create_task(
            asyncio.to_thread(
                self._write_serialized_state,
                serialized,
                revision,
            )
        )
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            # Resolve the write before propagating cancellation so callers
            # never have to guess whether the marker reached disk.
            await write_task
            raise

    def _record_inbound_message_processed(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str,
        text_fingerprint: str,
        response_payload: list[dict] | None = None,
    ) -> None:
        binding = self.get_binding(channel_id, conversation_id)
        if not message_id:
            key = (channel_id, conversation_id)
            bucket = self._recent_inbound_fingerprints.setdefault(key, {})
            bucket[f"{user_id}:{text_fingerprint}"] = self.clock()
            return
        recent = binding.reply_context.get("recent_inbound_message_ids")
        recent_ids = [str(item) for item in recent] if isinstance(recent, list) else []
        recent_ids = [item for item in recent_ids if item != message_id]
        recent_ids.append(message_id)
        binding.reply_context["recent_inbound_message_ids"] = recent_ids[-self.RECENT_INBOUND_MESSAGE_ID_LIMIT :]
        if response_payload is not None:
            responses = binding.reply_context.get("recent_inbound_responses")
            response_map = dict(responses) if isinstance(responses, dict) else {}
            response_map.pop(message_id, None)
            response_map[message_id] = copy.deepcopy(response_payload)
            overflow = len(response_map) - self.RECENT_INBOUND_RESPONSE_LIMIT
            for old_message_id in list(response_map)[: max(0, overflow)]:
                response_map.pop(old_message_id, None)
            binding.reply_context["recent_inbound_responses"] = response_map

    def get_processed_inbound_response(
        self,
        channel_id: str,
        conversation_id: str,
        message_id: str,
    ) -> list[dict] | None:
        binding = self._bindings.get((channel_id, conversation_id))
        if binding is None:
            return None
        responses = binding.reply_context.get("recent_inbound_responses")
        if not isinstance(responses, dict):
            return None
        payload = responses.get(message_id)
        return copy.deepcopy(payload) if isinstance(payload, list) else None

    def should_drop_duplicate_inbound_message(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str | None = None,
        text_fingerprint: str,
    ) -> bool:
        key = (channel_id, conversation_id)
        binding = self._bindings.get(key)
        if message_id and binding is not None:
            recent = binding.reply_context.get("recent_inbound_message_ids")
            if isinstance(recent, list) and message_id in {str(item) for item in recent}:
                return True
        now = self.clock()
        bucket = self._recent_inbound_fingerprints.setdefault(key, {})
        expired = [
            fingerprint for fingerprint, seen_at in bucket.items() if now - seen_at > self.INBOUND_DEDUP_WINDOW_S
        ]
        for fingerprint in expired:
            bucket.pop(fingerprint, None)
        if message_id:
            return False
        fingerprint = f"{user_id}:{text_fingerprint}"
        seen_at = bucket.get(fingerprint)
        return seen_at is not None and now - seen_at <= self.INBOUND_DEDUP_WINDOW_S

    def _save(self) -> None:
        if not self.state_path:
            return
        revision, serialized = self._snapshot_state()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_serialized_state(serialized, revision)
            return
        self._queued_state_write = (revision, serialized)
        if self._background_writer_task is None or self._background_writer_task.done():
            self._background_writer_task = loop.create_task(self._run_background_writer())

    async def _run_background_writer(self) -> None:
        try:
            while self._queued_state_write is not None:
                revision, serialized = self._queued_state_write
                self._queued_state_write = None
                if revision <= self._current_persisted_revision():
                    continue
                try:
                    await self._write_state_async(serialized, revision)
                except asyncio.CancelledError:
                    if revision > self._current_persisted_revision():
                        self._background_write_failures[revision] = asyncio.CancelledError()
                    raise
                except BaseException as exc:
                    self._background_write_failures[revision] = exc
                    logger.error(
                        "Bridge state background persistence failed: %s",
                        type(exc).__name__,
                    )
                self._discard_superseded_write_failures()
        finally:
            self._background_writer_task = None

    async def flush_pending_writes(self) -> None:
        while self._background_writer_task is not None:
            task = self._background_writer_task
            await asyncio.shield(task)
        self._discard_superseded_write_failures()
        persisted_revision = self._current_persisted_revision()
        outstanding = [
            (revision, error)
            for revision, error in self._background_write_failures.items()
            if revision > persisted_revision
        ]
        if outstanding:
            revision, error = max(outstanding, key=lambda item: item[0])
            raise RuntimeError(f"Could not persist bridge state revision {revision}") from error

    def _discard_superseded_write_failures(self) -> None:
        persisted_revision = self._current_persisted_revision()
        self._background_write_failures = {
            revision: error
            for revision, error in self._background_write_failures.items()
            if revision > persisted_revision
        }

    def _current_persisted_revision(self) -> int:
        with self._save_lock:
            return self._persisted_state_revision

    def _snapshot_state(self) -> tuple[int, str]:
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
                    "show_system": binding.show_system,
                    "reply_context": binding.reply_context,
                }
                for binding in self._bindings.values()
                if binding.thread_id is not None
                or binding.bootstrap_cwd is not None
                or binding.visibility_profile != "standard"
                or binding.show_commentary is not True
                or binding.show_toolcalls is not False
                or binding.show_system is not False
                or binding.reply_context
            ],
            "pending_requests": [],
            "pending_terminal_deliveries": [
                {
                    "thread_id": pending.thread_id,
                    "turn_id": pending.turn_id,
                    "message": pending.message,
                    "created_at": pending.created_at,
                }
                for pending in self._pending_terminal_deliveries.values()
            ],
        }
        with self._revision_lock:
            self._next_state_revision += 1
            revision = self._next_state_revision
        return revision, json.dumps(payload, ensure_ascii=True, indent=2) + "\n"

    def _write_serialized_state(self, serialized: str, revision: int) -> None:
        if self.state_path is None:
            return
        temporary: Path | None = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{self.state_path.name}.tmp.",
                dir=self.state_path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            # Slow file I/O happens before taking the shared lock. A normal
            # event-loop mutation therefore never waits behind a worker that
            # is blocked in fsync.
            with self._save_lock:
                if revision <= self._persisted_state_revision:
                    temporary.unlink()
                    return
                os.replace(temporary, self.state_path)
                self._persisted_state_revision = revision
        except BaseException:
            if revision <= self._persisted_state_revision:
                # A newer revision may have won while this writer was doing
                # I/O. Its state is authoritative.
                try:
                    if temporary is not None:
                        temporary.unlink()
                except FileNotFoundError:
                    pass
                return
            try:
                if temporary is not None:
                    temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not load bridge state: {self.state_path}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise RuntimeError(f"Unsupported or invalid bridge state: {self.state_path}")
        bindings = payload.get("bindings")
        if not isinstance(bindings, list):
            raise RuntimeError(f"Invalid bridge bindings state: {self.state_path}")
        for item in bindings:
            if not isinstance(item, dict) or "channel_id" not in item or "conversation_id" not in item:
                raise RuntimeError(f"Invalid bridge binding entry: {self.state_path}")
            binding = ConversationBinding(
                channel_id=str(item["channel_id"]),
                conversation_id=str(item["conversation_id"]),
                thread_id=str(item["thread_id"]) if item.get("thread_id") is not None else None,
                bootstrap_cwd=str(item["bootstrap_cwd"]) if item.get("bootstrap_cwd") is not None else None,
                visibility_profile=str(item.get("visibility_profile") or "standard"),
                show_commentary=bool(item.get("show_commentary", True)),
                show_toolcalls=bool(item.get("show_toolcalls", False)),
                show_system=bool(item.get("show_system", False)),
                reply_context=dict(item.get("reply_context") or {}),
            )
            self._bindings[(binding.channel_id, binding.conversation_id)] = binding
        pending_terminal_deliveries = payload.get("pending_terminal_deliveries", [])
        if not isinstance(pending_terminal_deliveries, list):
            raise RuntimeError(f"Invalid pending terminal delivery state: {self.state_path}")
        for item in pending_terminal_deliveries:
            if not isinstance(item, dict):
                raise RuntimeError(f"Invalid pending terminal delivery entry: {self.state_path}")
            thread_id = str(item.get("thread_id") or "")
            turn_id = str(item.get("turn_id") or "")
            message = item.get("message")
            if not thread_id or not turn_id or (message is not None and not isinstance(message, dict)):
                raise RuntimeError(f"Invalid pending terminal delivery entry: {self.state_path}")
            self._pending_terminal_deliveries[(thread_id, turn_id)] = PendingTerminalDelivery(
                thread_id=thread_id,
                turn_id=turn_id,
                message=copy.deepcopy(message),
                created_at=float(item.get("created_at") or 0.0),
            )

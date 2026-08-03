from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Lock, RLock

from .models import (
    ConversationBinding,
    LegacyPendingDeliveryEvidence,
    NativeThreadSnapshot,
    ThreadBrowserContext,
)
from .store_legacy_delivery_evidence import LegacyDeliveryEvidenceStoreMixin

Clock = Callable[[], float]
logger = logging.getLogger(__name__)


class ConversationStore(LegacyDeliveryEvidenceStoreMixin):
    def __init__(
        self,
        clock: Clock,
        state_path: str | Path | None = None,
    ) -> None:
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self._bindings: dict[tuple[str, str], ConversationBinding] = {}
        self._thread_recipient_routes: dict[str, tuple[str, str]] = {}
        self._legacy_delivery_evidence: dict[
            str, LegacyPendingDeliveryEvidence
        ] = {}
        self._thread_snapshots: dict[str, NativeThreadSnapshot] = {}
        self._thread_browser_contexts: dict[tuple[str, str], ThreadBrowserContext] = {}
        self._save_lock = RLock()
        self._revision_lock = Lock()
        self._next_state_revision = 0
        self._persisted_state_revision = 0
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

    def find_recipient_route_by_thread_id(
        self,
        thread_id: str,
    ) -> tuple[str, str] | None:
        """Return the last IM recipient that explicitly selected a native thread."""

        return self._thread_recipient_routes.get(str(thread_id or "").strip())

    def set_bootstrap_cwd(
        self, channel_id: str, conversation_id: str, cwd: str
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.bootstrap_cwd = cwd
        binding.thread_id = None
        self._save()
        return binding

    def bind_thread(
        self, channel_id: str, conversation_id: str, thread_id: str
    ) -> ConversationBinding:
        for key, existing in self._bindings.items():
            if key == (channel_id, conversation_id):
                continue
            if existing.thread_id == thread_id:
                existing.thread_id = None
        binding = self.get_binding(channel_id, conversation_id)
        binding.thread_id = thread_id
        self._thread_recipient_routes[thread_id] = (channel_id, conversation_id)
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

    def clear_thread_binding(
        self, channel_id: str, conversation_id: str
    ) -> ConversationBinding:
        binding = self.get_binding(channel_id, conversation_id)
        binding.thread_id = None
        self._save()
        return binding

    def note_thread_snapshot(
        self, snapshot: NativeThreadSnapshot
    ) -> NativeThreadSnapshot:
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

    def clear_thread_browser_context(
        self, channel_id: str, conversation_id: str
    ) -> None:
        self._thread_browser_contexts.pop((channel_id, conversation_id), None)

    def set_visibility_profile(
        self, channel_id: str, conversation_id: str, profile: str
    ) -> ConversationBinding:
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
            self._background_writer_task = loop.create_task(
                self._run_background_writer()
            )

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
                        self._background_write_failures[revision] = (
                            asyncio.CancelledError()
                        )
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
            raise RuntimeError(
                f"Could not persist bridge state revision {revision}"
            ) from error

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
            "thread_recipient_routes": [
                {
                    "thread_id": thread_id,
                    "channel_id": route[0],
                    "conversation_id": route[1],
                }
                for thread_id, route in sorted(self._thread_recipient_routes.items())
            ],
            "pending_terminal_deliveries": [
                {
                    "delivery_id": pending.delivery_id,
                    "thread_id": pending.thread_id,
                    "turn_id": pending.turn_id,
                    "message": pending.message,
                    "created_at": pending.created_at,
                    "sequence": pending.sequence,
                }
                for pending in self._legacy_delivery_evidence.values()
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
            raise RuntimeError(
                f"Could not load bridge state: {self.state_path}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise RuntimeError(
                f"Unsupported or invalid bridge state: {self.state_path}"
            )
        bindings = payload.get("bindings")
        if not isinstance(bindings, list):
            raise RuntimeError(f"Invalid bridge bindings state: {self.state_path}")
        for item in bindings:
            if (
                not isinstance(item, dict)
                or "channel_id" not in item
                or "conversation_id" not in item
            ):
                raise RuntimeError(f"Invalid bridge binding entry: {self.state_path}")
            binding = ConversationBinding(
                channel_id=str(item["channel_id"]),
                conversation_id=str(item["conversation_id"]),
                thread_id=str(item["thread_id"])
                if item.get("thread_id") is not None
                else None,
                bootstrap_cwd=str(item["bootstrap_cwd"])
                if item.get("bootstrap_cwd") is not None
                else None,
                visibility_profile=str(item.get("visibility_profile") or "standard"),
                show_commentary=bool(item.get("show_commentary", True)),
                show_toolcalls=bool(item.get("show_toolcalls", False)),
                show_system=bool(item.get("show_system", False)),
                reply_context=dict(item.get("reply_context") or {}),
            )
            self._bindings[(binding.channel_id, binding.conversation_id)] = binding
        thread_recipient_routes = payload.get("thread_recipient_routes", [])
        if not isinstance(thread_recipient_routes, list):
            raise RuntimeError(
                f"Invalid thread recipient routes state: {self.state_path}"
            )
        for item in thread_recipient_routes:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Invalid thread recipient route entry: {self.state_path}"
                )
            thread_id = str(item.get("thread_id") or "").strip()
            channel_id = str(item.get("channel_id") or "").strip()
            conversation_id = str(item.get("conversation_id") or "").strip()
            if not thread_id or not channel_id or not conversation_id:
                raise RuntimeError(
                    f"Invalid thread recipient route entry: {self.state_path}"
                )
            self._thread_recipient_routes[thread_id] = (
                channel_id,
                conversation_id,
            )
        # State written before standalone delivery routes existed can derive
        # the only truthful route available from its current bindings.
        for binding in self._bindings.values():
            if binding.thread_id:
                self._thread_recipient_routes[binding.thread_id] = (
                    binding.channel_id,
                    binding.conversation_id,
                )
        pending_terminal_deliveries = payload.get("pending_terminal_deliveries", [])
        if not isinstance(pending_terminal_deliveries, list):
            raise RuntimeError(
                f"Invalid pending terminal delivery state: {self.state_path}"
            )
        for legacy_sequence, item in enumerate(pending_terminal_deliveries, start=1):
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Invalid pending terminal delivery entry: {self.state_path}"
                )
            thread_id = str(item.get("thread_id") or "")
            turn_id = str(item.get("turn_id") or "")
            message = item.get("message")
            if bool(thread_id) != bool(turn_id) or (
                message is not None and not isinstance(message, dict)
            ):
                raise RuntimeError(
                    f"Invalid pending terminal delivery entry: {self.state_path}"
                )
            if message is None:
                # Pre-split terminal watches are obsolete and contain no IM
                # payload that the one-way SDK migration can submit.
                continue
            metadata = message.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            delivery_id = str(
                item.get("delivery_id") or metadata.get("delivery_id") or ""
            )
            if not delivery_id:
                canonical = json.dumps(
                    message, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                )
                delivery_id = f"imcodex:legacy-terminal:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
            sequence = int(item.get("sequence") or legacy_sequence)
            self._legacy_delivery_evidence[delivery_id] = (
                LegacyPendingDeliveryEvidence(
                    delivery_id=delivery_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message=copy.deepcopy(message),
                    created_at=float(item.get("created_at") or 0.0),
                    sequence=sequence,
                )
            )

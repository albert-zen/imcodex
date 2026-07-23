from __future__ import annotations

import copy
import os
import re

from ..models import InboundAttachment, NativeThreadSnapshot
from ..observability.runtime import emit_event
from .backend_types import (
    ACTIVE_THREAD_STATUSES,
    StaleThreadBindingError,
    ThreadListResult,
    ThreadSelectionError,
    TurnSubmission,
)
from .client import AppServerError


_TERMINAL_TURN_STATUSES = frozenset({"completed", "interrupted", "failed"})
_IMAGE_ONLY_DISPLAY_TEXT = "[Image]"
_ATTACHMENT_ONLY_DISPLAY_TEXT = "[Attachment]"
_ATTACHMENTS_DISPLAY_TEXT = "[Attachments]"
_THREAD_LIST_BATCH_SIZE = 100


class CodexThreadBackendMixin:
    def native_dispatch_sequence(self) -> int:
        try:
            return int(getattr(self.client, "last_received_dispatch_sequence", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def supports_local_image_paths(self) -> bool:
        capability = getattr(self.client, "supports_local_image_paths", None)
        if callable(capability):
            return bool(capability())
        if capability is not None:
            return bool(capability)
        return False

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
                )
            except AppServerError as exc:
                if self._is_stale_thread_error(exc):
                    raise StaleThreadBindingError(binding.thread_id) from exc
                raise
            snapshot = self._remember_snapshot(result.get("thread") or {})
            if snapshot.thread_id != binding.thread_id:
                raise AppServerError(
                    "Codex resumed a different thread; refusing an inexact continuation"
                )
            self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
            self._reconcile_native_active_turn(result.get("thread") or {}, snapshot)
            if (
                str(snapshot.status or "").strip().lower() in ACTIVE_THREAD_STATUSES
                and self.store.get_active_turn(snapshot.thread_id) is None
            ):
                raise AppServerError(
                    "Codex reports this thread as active but did not expose its active turn; "
                    "refusing to start a competing turn"
                )
            return snapshot.thread_id
        if binding.bootstrap_cwd is None:
            raise KeyError("No working directory selected for thread session")
        result = await self.client.start_thread(
            cwd=binding.bootstrap_cwd,
            service_name=self.service_name,
            **self._new_thread_dynamic_tool_params(),
        )
        snapshot = self._remember_snapshot(result.get("thread") or {})
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        return snapshot.thread_id

    def _new_thread_dynamic_tool_params(self) -> dict[str, object]:
        dynamic_tools = getattr(self, "thread_dynamic_tools", None)
        if not dynamic_tools:
            return {}
        return {"dynamicTools": copy.deepcopy(dynamic_tools)}

    async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
        result = await self.client.resume_thread(
            thread_id=thread_id,
            service_name=self.service_name,
        )
        payload = result.get("thread")
        if not isinstance(payload, dict):
            raise AppServerError(f"thread {thread_id} is not available in Codex")
        returned_thread_id = str(payload.get("id") or payload.get("threadId") or "")
        if returned_thread_id != thread_id:
            raise AppServerError(
                "Codex resumed a different thread; refusing an inexact handoff"
            )
        native_status = self._native_status(payload.get("status"))
        native_active = self._native_active_turn(payload)
        if (
            native_status is not None
            and native_status.strip().lower() in ACTIVE_THREAD_STATUSES
            and native_active is None
        ):
            raise AppServerError(
                "Codex reports this thread as active but did not expose its active turn; "
                "refusing an unverifiable handoff"
            )
        snapshot = self._remember_snapshot(payload)
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        self._reconcile_native_active_turn(payload, snapshot)
        return snapshot.thread_id

    async def resolve_thread_selector(
        self,
        channel_id: str,
        conversation_id: str,
        selector: str,
    ) -> NativeThreadSnapshot:
        normalized_selector = self._normalize_selector(selector)
        if not normalized_selector:
            raise ThreadSelectionError("Enter a thread name, preview, or ID.")
        threads = await self.list_threads(channel_id, conversation_id)
        ranked: list[tuple[int, int, NativeThreadSnapshot]] = []
        for index, snapshot in enumerate(threads):
            score = self._thread_match_score(snapshot, selector)
            if score is not None:
                ranked.append((score, index, snapshot))
        if not ranked:
            raise ThreadSelectionError(f"No thread matches '{selector}'. Try /threads {selector}.")
        ranked.sort(key=lambda item: (item[0], item[1]))
        best_score = ranked[0][0]
        best_matches = [snapshot for score, _, snapshot in ranked if score == best_score]
        if len(best_matches) > 1:
            labels = ", ".join(self._thread_short_label(snapshot) for snapshot in best_matches[:3])
            if len(best_matches) > 3:
                labels += ", ..."
            raise ThreadSelectionError(
                f"'{selector}' matches multiple threads: {labels}. Try /threads {selector}."
            )
        return best_matches[0]

    async def list_threads(
        self,
        channel_id: str,
        conversation_id: str,
    ) -> list[NativeThreadSnapshot]:
        result = await self.query_threads(channel_id, conversation_id)
        return result.threads

    async def query_threads(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        search_term: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ThreadListResult:
        preferred_cwd = self.store.current_cwd(channel_id, conversation_id)
        params: dict[str, object] = {"sortKey": "updated_at"}
        if search_term:
            params["searchTerm"] = search_term
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        result = await self.client.list_threads(**params)
        threads = [self._remember_snapshot(item) for item in self._thread_list_items(result)]
        binding = self.store.get_binding(channel_id, conversation_id)
        next_cursor = self._next_thread_cursor(result)
        seen_thread_ids = {snapshot.thread_id for snapshot in threads}
        if (
            not search_term
            and cursor is None
            and next_cursor is None
            and (limit is None or len(threads) < limit)
            and binding.thread_id
            and binding.thread_id not in seen_thread_ids
        ):
            snapshot = await self.read_thread(channel_id, conversation_id, binding.thread_id)
            if snapshot is not None:
                threads.append(snapshot)
        return ThreadListResult(
            threads=self._prioritize_threads(
                threads,
                bound_thread_id=binding.thread_id,
                preferred_cwd=preferred_cwd,
            ),
            next_cursor=next_cursor,
        )

    async def query_all_threads(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        search_term: str | None = None,
    ) -> ThreadListResult:
        """Read the complete native thread catalog for a short-lived browser view."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        thread_order: list[str] = []
        threads_by_id: dict[str, NativeThreadSnapshot] = {}
        while True:
            params: dict[str, object] = {
                "sortKey": "updated_at",
                "limit": _THREAD_LIST_BATCH_SIZE,
            }
            if search_term:
                params["searchTerm"] = search_term
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.client.list_threads(**params)
            for item in self._thread_list_items(result):
                snapshot = self._remember_snapshot(item)
                if snapshot.thread_id not in threads_by_id:
                    thread_order.append(snapshot.thread_id)
                threads_by_id[snapshot.thread_id] = snapshot
            next_cursor = self._next_thread_cursor(result)
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise AppServerError("thread list returned a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        threads = [threads_by_id[thread_id] for thread_id in thread_order]
        binding = self.store.get_binding(channel_id, conversation_id)
        if not search_term and binding.thread_id:
            seen_thread_ids = {snapshot.thread_id for snapshot in threads}
            if binding.thread_id not in seen_thread_ids:
                snapshot = await self.read_thread(channel_id, conversation_id, binding.thread_id)
                if snapshot is not None:
                    threads.append(snapshot)
        return ThreadListResult(
            threads=self._prioritize_threads(
                threads,
                bound_thread_id=binding.thread_id,
                preferred_cwd=None,
            ),
            next_cursor=None,
        )

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
        snapshot = self._remember_snapshot(payload)
        self._reconcile_native_active_turn(payload, snapshot)
        return snapshot

    async def read_thread_history(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        limit: int = 6,
        page: int = 1,
    ) -> dict:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        safe_limit = max(1, int(limit))
        safe_page = max(1, int(page))
        try:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for page_number in range(1, safe_page + 1):
                page_options: dict[str, object] = {
                    "limit": safe_limit,
                    "items_view": "full",
                    "sort_direction": "desc",
                }
                if cursor is not None:
                    page_options["cursor"] = cursor
                payload = await self.client.list_thread_turns(thread_id, **page_options)
                turns = self._history_turn_items(payload)
                next_cursor = self._next_thread_cursor(payload)
                if page_number == safe_page:
                    return {
                        "turns": list(reversed(turns)),
                        "page": safe_page,
                        "hasOlder": next_cursor is not None,
                    }
                if next_cursor is None:
                    return {"turns": [], "page": safe_page, "hasOlder": False}
                if next_cursor in seen_cursors:
                    raise AppServerError("thread history returned a repeated pagination cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except AppServerError as exc:
            if not self._is_unsupported_method_error(exc):
                raise
        payload = await self.client.read_thread(thread_id, include_turns=True)
        turns = self._history_turn_items(payload)
        end = max(0, len(turns) - ((safe_page - 1) * safe_limit))
        start = max(0, end - safe_limit)
        return {
            "turns": turns[start:end],
            "page": safe_page,
            "hasOlder": start > 0,
        }

    async def fork_thread(self, channel_id: str, conversation_id: str) -> NativeThreadSnapshot:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        result = await self.client.fork_thread(thread_id)
        payload = result.get("thread")
        if not isinstance(payload, dict):
            forked_id = result.get("threadId")
            if forked_id is None:
                raise AppServerError("Codex did not return a forked thread")
            payload = {"id": forked_id}
        snapshot = self._remember_snapshot(payload)
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        return snapshot

    async def rename_thread(self, channel_id: str, conversation_id: str, name: str) -> dict:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        result = await self.client.set_thread_name(thread_id, name)
        payload = result.get("thread")
        if isinstance(payload, dict):
            self._remember_snapshot(payload)
        return result

    async def compact_thread(self, channel_id: str, conversation_id: str) -> dict:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        return await self.client.compact_thread(thread_id)

    async def read_thread_goal(self, channel_id: str, conversation_id: str) -> dict:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return {"goal": None}
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        return await self.client.get_thread_goal(thread_id)

    async def set_thread_goal(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
    ) -> dict:
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        if objective is not None:
            # Native goal replacement clears first so accounting and budgets do not carry over.
            await self.client.clear_thread_goal(thread_id)
        return await self.client.set_thread_goal(
            thread_id,
            objective=objective,
            status=status,
        )

    async def clear_thread_goal(self, channel_id: str, conversation_id: str) -> dict:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return {"cleared": False}
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        return await self.client.clear_thread_goal(thread_id)

    async def submit_input(
        self,
        channel_id: str,
        conversation_id: str,
        text: str,
        attachments: tuple[InboundAttachment, ...] = (),
    ) -> TurnSubmission:
        if attachments and not self.supports_local_image_paths():
            raise AppServerError("configured App Server cannot read bridge-local attachment paths")
        expected_local_image_epoch: int | None = None
        if attachments:
            epoch_capability = getattr(self.client, "local_image_paths_epoch", None)
            if callable(epoch_capability):
                expected_local_image_epoch = epoch_capability()
                if expected_local_image_epoch is None:
                    initialize = getattr(self.client, "initialize", None)
                    if callable(initialize):
                        await initialize()
                        expected_local_image_epoch = epoch_capability()
                    if expected_local_image_epoch is None:
                        raise AppServerError(
                            "configured App Server cannot read bridge-local attachment paths for this connection"
                        )
        input_items = self._native_user_input(text, attachments)
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is not None:
            can_resume = callable(getattr(self.client, "resume_thread", None))
            thread_id = binding.thread_id
            active = self.store.get_active_turn(thread_id)
            attempted_steer = active is not None and active[1] == "inProgress"

            # Keep the live App Server connection authoritative for a Turn we
            # already observed. Resuming first can race with a just-created
            # rollout and, across clients, can replace useful live state with a
            # persisted snapshot before native turn/steer gets a chance.
            if not attempted_steer and can_resume:
                thread_id = await self.ensure_thread(channel_id, conversation_id)
                expected_local_image_epoch = self._refresh_local_image_epoch(
                    expected_local_image_epoch
                )
            try:
                return await self._submit_to_reconciled_thread(
                    thread_id,
                    input_items,
                    expected_local_image_epoch=expected_local_image_epoch,
                )
            except AppServerError as exc:
                if not can_resume or not self._requires_thread_resume(exc):
                    raise
                thread_id = await self.ensure_thread(channel_id, conversation_id)
                expected_local_image_epoch = self._refresh_local_image_epoch(
                    expected_local_image_epoch
                )
                # Native uses the same invalid-request class for both stale and
                # temporarily non-steerable Turns. If resume still exposes an
                # active Turn, preserve the authoritative steer rejection rather
                # than risking a competing turn/start.
                if attempted_steer and self.store.get_active_turn(thread_id) is not None:
                    raise exc
                return await self._submit_to_reconciled_thread(
                    thread_id,
                    input_items,
                    expected_local_image_epoch=expected_local_image_epoch,
                )
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        expected_local_image_epoch = self._refresh_local_image_epoch(
            expected_local_image_epoch
        )
        return await self._start_turn(
            thread_id,
            input_items,
            expected_local_image_epoch=expected_local_image_epoch,
        )

    def _refresh_local_image_epoch(self, current: int | None) -> int | None:
        if current is None:
            return None
        capability = getattr(self.client, "local_image_paths_epoch", None)
        if not callable(capability):
            return current
        refreshed = capability()
        if refreshed is None:
            raise AppServerError(
                "configured App Server cannot read bridge-local attachment paths for this connection"
            )
        return int(refreshed)

    async def _submit_to_reconciled_thread(
        self,
        thread_id: str,
        input_items: list[dict[str, object]],
        *,
        expected_local_image_epoch: int | None,
    ) -> TurnSubmission:
        active = self.store.get_active_turn(thread_id)
        if active is not None and active[1] == "inProgress":
            try:
                steer_kwargs: dict[str, object] = {"input_items": input_items}
                if expected_local_image_epoch is not None:
                    steer_kwargs["expected_local_image_epoch"] = expected_local_image_epoch
                await self.client.steer_turn(thread_id, active[0], **steer_kwargs)
            except AppServerError as exc:
                if not self._is_stale_turn_error(exc):
                    raise
                if callable(getattr(self.client, "resume_thread", None)):
                    raise
                self.store.clear_active_turn(thread_id)
            else:
                return TurnSubmission(kind="steer", thread_id=thread_id, turn_id=active[0])
        return await self._start_turn(
            thread_id,
            input_items,
            expected_local_image_epoch=expected_local_image_epoch,
        )

    async def submit_text(self, channel_id: str, conversation_id: str, text: str) -> TurnSubmission:
        return await self.submit_input(channel_id, conversation_id, text)

    @staticmethod
    def _native_user_input(
        text: str,
        attachments: tuple[InboundAttachment, ...],
    ) -> list[dict[str, object]]:
        input_items: list[dict[str, object]] = []
        file_attachments = tuple(item for item in attachments if item.kind == "file")
        durable_text = CodexThreadBackendMixin._with_file_attachment_manifest(
            text,
            file_attachments,
        )
        if durable_text.strip():
            input_items.append({"type": "text", "text": durable_text})
        elif attachments:
            # Codex App currently omits user messages whose native input has no
            # text item, even when localImage items are present. Keep the image
            # native while adding the smallest visible cross-surface caption.
            display_text = (
                _IMAGE_ONLY_DISPLAY_TEXT
                if all(item.kind == "image" for item in attachments)
                else _ATTACHMENT_ONLY_DISPLAY_TEXT
            )
            input_items.append({"type": "text", "text": display_text})
        for attachment in attachments:
            if attachment.kind == "image":
                input_items.append({"type": "localImage", "path": attachment.local_path})
            elif attachment.kind == "file":
                input_items.append(
                    {
                        "type": "mention",
                        "name": CodexThreadBackendMixin._safe_attachment_filename(
                            attachment
                        ),
                        "path": attachment.local_path,
                    }
                )
            else:
                raise ValueError(f"unsupported inbound attachment kind: {attachment.kind}")
        if not input_items:
            raise ValueError("inbound message has no supported input")
        return input_items

    @staticmethod
    def _with_file_attachment_manifest(
        text: str,
        attachments: tuple[InboundAttachment, ...],
    ) -> str:
        if not attachments:
            return text
        header = (
            _ATTACHMENT_ONLY_DISPLAY_TEXT
            if len(attachments) == 1
            else _ATTACHMENTS_DISPLAY_TEXT
        )
        lines = [header]
        for attachment in attachments:
            path = str(attachment.local_path)
            if not path or any(
                ord(character) < 32 or ord(character) == 127 for character in path
            ):
                raise ValueError(
                    "inbound attachment path contains unsupported control characters"
                )
            lines.extend(
                (
                    f"- {CodexThreadBackendMixin._safe_attachment_filename(attachment)}",
                    f"  Path: {path}",
                )
            )
        manifest = "\n".join(lines)
        if not text.strip():
            return manifest
        if text.endswith(_ATTACHMENT_ONLY_DISPLAY_TEXT):
            prefix = text[: -len(_ATTACHMENT_ONLY_DISPLAY_TEXT)]
            if not prefix or prefix.endswith("\n"):
                return f"{prefix}{manifest}"
        if text.endswith("\n\n"):
            separator = ""
        elif text.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        return f"{text}{separator}{manifest}"

    @staticmethod
    def _safe_attachment_filename(attachment: InboundAttachment) -> str:
        candidate = (
            str(attachment.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        )
        candidate = "".join(
            character
            for character in candidate
            if character >= " " and character != "\x7f"
        ).strip(" .")
        if candidate:
            return candidate[:120]
        fallback = (
            str(attachment.local_path).replace("\\", "/").rsplit("/", 1)[-1]
        )
        return fallback[:120] or "attachment"

    async def _start_turn(
        self,
        thread_id: str,
        input_items: list[dict[str, object]],
        *,
        expected_local_image_epoch: int | None = None,
    ) -> TurnSubmission:
        start_kwargs: dict[str, object] = {
            "thread_id": thread_id,
            "input_items": input_items,
            "summary": "concise",
        }
        if expected_local_image_epoch is not None:
            start_kwargs["expected_local_image_epoch"] = expected_local_image_epoch
        result = await self.client.start_turn(**start_kwargs)
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
        return await self.interrupt_turn(binding.thread_id, active[0])

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> bool:
        try:
            await self.client.interrupt_turn(thread_id, turn_id)
        except AppServerError as exc:
            if not self._is_stale_turn_error(exc):
                raise
            self.store.suppress_turn(thread_id, turn_id)
            self.store.discard_terminal_watch(thread_id, turn_id)
            self.store.clear_active_turn(thread_id)
            self.store.remove_pending_requests_for_turn(thread_id, turn_id)
            return False
        self.store.suppress_turn(thread_id, turn_id)
        self.store.discard_terminal_watch(thread_id, turn_id)
        self.store.clear_active_turn(thread_id)
        self.store.remove_pending_requests_for_turn(thread_id, turn_id)
        return True

    async def rehydrate_bound_threads(self) -> dict:
        summary = {"total": 0, "succeeded": 0, "failed": 0, "unverified": 0}
        recovered_turns: list[dict] = []
        discarded_turns: list[dict[str, str]] = []
        for binding in self.store.iter_bindings():
            if not binding.thread_id:
                continue
            summary["total"] += 1
            watched_deliveries = {
                pending.turn_id: pending
                for pending in self.store.list_pending_terminal_deliveries(binding.thread_id)
            }
            cached_active = self.store.get_active_turn(binding.thread_id)
            if cached_active is not None:
                # Cached turn state is not authoritative across a transport
                # epoch. Clear it before resume so a replayed native request
                # cannot be rejected as belonging to a different local turn.
                self.store.clear_active_turn(binding.thread_id)
            emit_event(
                component="appserver.backend",
                event="bridge.thread_rehydrate.started",
                message="Rehydrating bound thread",
                data={
                    "channel_id": binding.channel_id,
                    "conversation_id": binding.conversation_id,
                    "thread_id": binding.thread_id,
                },
            )
            try:
                resume = getattr(
                    self.client,
                    "resume_thread_for_recovery",
                    self.client.resume_thread,
                )
                result = await resume(
                    thread_id=binding.thread_id,
                    service_name=self.service_name,
                )
            except AppServerError as exc:
                summary["failed"] += 1
                failed_thread_id = binding.thread_id
                stale_thread = self._is_stale_thread_error(exc)
                had_active_turn = cached_active is not None
                if cached_active is not None:
                    discarded_turns.append(
                        {"threadId": failed_thread_id, "turnId": cached_active[0]}
                    )
                if stale_thread:
                    self.store.clear_thread_binding(binding.channel_id, binding.conversation_id)
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.failed",
                    level="WARNING",
                    message=str(exc),
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": failed_thread_id,
                        "error_type": type(exc).__name__,
                        "cleared_active_turn": had_active_turn,
                    },
                )
                continue
            payload = result.get("thread")
            if not isinstance(payload, dict):
                summary["unverified"] += 1
                had_active_turn = cached_active is not None
                if cached_active is not None:
                    discarded_turns.append(
                        {"threadId": binding.thread_id, "turnId": cached_active[0]}
                    )
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.empty",
                    level="WARNING",
                    message="Thread resume returned no thread payload",
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": binding.thread_id,
                        "cleared_active_turn": had_active_turn,
                    },
                )
                continue
            returned_thread_id = str(payload.get("id") or payload.get("threadId") or "")
            native_thread_status = self._native_status(payload.get("status"))
            if returned_thread_id != binding.thread_id or native_thread_status is None:
                summary["unverified"] += 1
                had_active_turn = cached_active is not None
                if cached_active is not None:
                    discarded_turns.append(
                        {"threadId": binding.thread_id, "turnId": cached_active[0]}
                    )
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.unverified",
                    level="WARNING",
                    message="Thread resume returned unverifiable native state",
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": binding.thread_id,
                        "returned_thread_id": returned_thread_id,
                        "has_native_status": native_thread_status is not None,
                        "cleared_active_turn": had_active_turn,
                    },
                )
                continue
            snapshot = self._remember_snapshot(payload)
            self.store.bind_thread_with_cwd(
                binding.channel_id,
                binding.conversation_id,
                snapshot.thread_id,
                snapshot.cwd,
            )
            native_active = self._native_active_turn(payload)
            native_thread_is_active = (
                native_thread_status.strip().lower() in ACTIVE_THREAD_STATUSES
            )
            if native_thread_is_active and native_active is None:
                summary["unverified"] += 1
                if cached_active is not None:
                    discarded_turns.append(
                        {"threadId": snapshot.thread_id, "turnId": cached_active[0]}
                    )
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.active_turn_unverified",
                    level="WARNING",
                    message="Active native thread did not expose a verifiable active turn",
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": snapshot.thread_id,
                    },
                )
                continue
            if native_active is not None:
                if cached_active is not None and cached_active[0] != native_active[0]:
                    self.store.suppress_turn(snapshot.thread_id, cached_active[0])
                    discarded_turns.append(
                        {"threadId": snapshot.thread_id, "turnId": cached_active[0]}
                    )
                self.store.note_active_turn(snapshot.thread_id, native_active[0], native_active[1])
            recovery_turn_ids = set(watched_deliveries)
            if cached_active is not None:
                recovery_turn_ids.add(cached_active[0])
            if native_active is not None:
                recovery_turn_ids.discard(native_active[0])
            recovery_unverified = False
            for recovery_turn_id in recovery_turn_ids:
                pending_delivery = watched_deliveries.get(recovery_turn_id)
                if pending_delivery is not None and pending_delivery.message is not None:
                    # The native result was already projected before the
                    # process stopped. The delivery outbox owns its retry.
                    continue
                terminal_turn = self._turn_by_id(payload, recovery_turn_id)
                if terminal_turn is None or not self._turn_is_terminal(terminal_turn):
                    summary["unverified"] += 1
                    recovery_unverified = True
                    discarded_turns.append(
                        {"threadId": snapshot.thread_id, "turnId": recovery_turn_id}
                    )
                    emit_event(
                        component="appserver.backend",
                        event="bridge.thread_rehydrate.delivery_turn_unverified",
                        level="WARNING",
                        message="Native resume did not verify a pending terminal delivery turn",
                        data={
                            "channel_id": binding.channel_id,
                            "conversation_id": binding.conversation_id,
                            "thread_id": snapshot.thread_id,
                            "turn_id": recovery_turn_id,
                        },
                    )
                    continue
                self.store.suppress_turn(snapshot.thread_id, recovery_turn_id)
                recovered_turns.append(
                    {"threadId": snapshot.thread_id, "turn": terminal_turn}
                )
            if recovery_unverified:
                continue
            emit_event(
                component="appserver.backend",
                event="bridge.thread_rehydrate.succeeded",
                message="Rehydrated bound thread",
                data={
                    "channel_id": binding.channel_id,
                    "conversation_id": binding.conversation_id,
                    "thread_id": snapshot.thread_id,
                    "cwd": snapshot.cwd,
                },
            )
            summary["succeeded"] += 1
        return {
            "summary": summary,
            "recoveredTurns": recovered_turns,
            "discardedTurns": discarded_turns,
        }

    def _remember_snapshot(self, payload: dict) -> NativeThreadSnapshot:
        status = self._native_status(payload.get("status"))
        thread_id = str(payload.get("id") or payload.get("threadId") or "")
        previous = self.store.get_thread_snapshot(thread_id)
        snapshot = NativeThreadSnapshot(
            thread_id=thread_id,
            cwd=str(payload.get("cwd") or (previous.cwd if previous is not None else "") or ""),
            preview=str(payload.get("preview") or (previous.preview if previous is not None else "") or ""),
            status=str(status or (previous.status if previous is not None else "idle") or "idle"),
            name=(
                str(payload["name"])
                if payload.get("name") is not None
                else (previous.name if previous is not None else None)
            ),
            path=(
                str(payload["path"])
                if payload.get("path") is not None
                else (previous.path if previous is not None else None)
            ),
            source=(
                str(payload["source"])
                if payload.get("source") is not None
                else (previous.source if previous is not None else None)
            ),
        )
        self.store.note_thread_snapshot(snapshot)
        return snapshot

    def _native_active_turn(self, payload: dict) -> tuple[str, str] | None:
        turns = payload.get("turns")
        if not isinstance(turns, list):
            return None
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id") or turn.get("turnId") or "")
            status = self._native_status(turn.get("status"))
            if turn_id and status is not None and status.strip().lower() in ACTIVE_THREAD_STATUSES:
                return turn_id, status
        return None

    def _reconcile_native_active_turn(
        self,
        payload: dict,
        snapshot: NativeThreadSnapshot,
    ) -> None:
        native_active = self._native_active_turn(payload)
        if native_active is not None:
            self.store.note_active_turn(snapshot.thread_id, native_active[0], native_active[1])
        elif str(snapshot.status or "").strip().lower() not in ACTIVE_THREAD_STATUSES:
            self.store.clear_active_turn(snapshot.thread_id)

    def _turn_by_id(self, payload: dict, turn_id: str) -> dict | None:
        turns = payload.get("turns")
        if not isinstance(turns, list):
            return None
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            candidate = str(turn.get("id") or turn.get("turnId") or "")
            if candidate == turn_id:
                return turn
        return None

    def _turn_is_terminal(self, turn: dict) -> bool:
        status = self._native_status(turn.get("status"))
        return status is not None and status.strip().lower() in _TERMINAL_TURN_STATUSES

    def _native_status(self, value: object) -> str | None:
        if isinstance(value, dict):
            value = value.get("type") or value.get("status")
        if value is None:
            return None
        status = str(value).strip()
        return status or None

    def _prioritize_threads(
        self,
        threads: list[NativeThreadSnapshot],
        *,
        bound_thread_id: str | None,
        preferred_cwd: str | None,
    ) -> list[NativeThreadSnapshot]:
        ranked: list[tuple[int, int, NativeThreadSnapshot]] = []
        for index, snapshot in enumerate(threads):
            priority = 2
            if bound_thread_id and snapshot.thread_id == bound_thread_id:
                priority = 0
            elif preferred_cwd and self._same_path(snapshot.cwd, preferred_cwd):
                priority = 1
            ranked.append((priority, index, snapshot))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [snapshot for _, _, snapshot in ranked]

    def _same_path(self, left: str, right: str) -> bool:
        return self._normalize_path(left) == self._normalize_path(right)

    def _normalize_path(self, value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        return os.path.normcase(os.path.normpath(normalized))

    def _thread_list_items(self, payload: dict) -> list[dict]:
        for key in ("threads", "data"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def _next_thread_cursor(self, payload: dict) -> str | None:
        for key in ("nextCursor", "next_cursor"):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    def _history_turn_items(self, payload: dict) -> list[dict]:
        for key in ("turns", "data"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        thread = payload.get("thread")
        if isinstance(thread, dict):
            turns = thread.get("turns")
            if isinstance(turns, list):
                return [item for item in turns if isinstance(item, dict)]
        return []

    def _history_turn_completed(self, turn: dict) -> bool:
        status = self._native_status(turn.get("status"))
        return status is not None and status.strip().lower() == "completed"

    def _active_thread_id(self, channel_id: str, conversation_id: str) -> str:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            raise KeyError("No active thread.")
        return binding.thread_id

    def _thread_match_score(self, snapshot: NativeThreadSnapshot, selector: str) -> int | None:
        selector_norm = self._normalize_selector(selector)
        if not selector_norm:
            return None
        thread_id = snapshot.thread_id.strip()
        if selector.strip() == thread_id:
            return 0
        best: int | None = None
        for label in self._thread_selector_labels(snapshot):
            normalized = self._normalize_selector(label)
            if not normalized:
                continue
            if normalized == selector_norm:
                return 1
            if normalized.startswith(selector_norm):
                best = self._min_score(best, 2)
                continue
            if any(token.startswith(selector_norm) for token in normalized.split()):
                best = self._min_score(best, 3)
                continue
            if selector_norm in normalized:
                best = self._min_score(best, 4)
        thread_id_norm = self._normalize_selector(thread_id)
        if thread_id_norm.startswith(selector_norm):
            best = self._min_score(best, 5)
        if selector_norm in thread_id_norm:
            best = self._min_score(best, 6)
        return best

    def _thread_selector_labels(self, snapshot: NativeThreadSnapshot) -> list[str]:
        labels = [snapshot.name or "", snapshot.preview or ""]
        location = snapshot.path or snapshot.cwd
        if location:
            labels.append(os.path.basename(location.rstrip("/\\")))
            labels.append(location)
        return labels

    def _thread_short_label(self, snapshot: NativeThreadSnapshot) -> str:
        label = snapshot.name or snapshot.preview or os.path.basename((snapshot.path or snapshot.cwd).rstrip("/\\")) or snapshot.thread_id
        return label.strip() or snapshot.thread_id

    def _normalize_selector(self, value: str) -> str:
        lowered = value.strip().lower()
        lowered = lowered.replace("_", " ").replace("-", " ")
        lowered = re.sub(r"\s+", " ", lowered)
        return lowered

    def _min_score(self, current: int | None, candidate: int) -> int:
        return candidate if current is None else min(current, candidate)

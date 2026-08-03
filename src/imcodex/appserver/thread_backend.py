from __future__ import annotations

import os
import re

from imagent.applications.appserver_client import AppServerError

from ..models import NativeThreadSnapshot
from .backend_types import (
    ACTIVE_THREAD_STATUSES,
    StaleThreadBindingError,
    ThreadListResult,
    ThreadSelectionError,
)

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
        previous = self.store.get_binding(channel_id, conversation_id)
        if previous.thread_id:
            self._unpersisted_thread_ids.discard(previous.thread_id)
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
            self.store.bind_thread_with_cwd(
                channel_id, conversation_id, snapshot.thread_id, snapshot.cwd
            )
            self._unpersisted_thread_ids.discard(snapshot.thread_id)
            return snapshot.thread_id
        if binding.bootstrap_cwd is None:
            raise KeyError("No working directory selected for thread session")
        result = await self.client.start_thread(
            cwd=binding.bootstrap_cwd,
            service_name=self.service_name,
        )
        snapshot = self._remember_snapshot(result.get("thread") or {})
        self.store.bind_thread_with_cwd(
            channel_id, conversation_id, snapshot.thread_id, snapshot.cwd
        )
        self._unpersisted_thread_ids.add(snapshot.thread_id)
        return snapshot.thread_id

    async def attach_thread(
        self, channel_id: str, conversation_id: str, thread_id: str
    ) -> str:
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
        self.store.bind_thread_with_cwd(
            channel_id, conversation_id, snapshot.thread_id, snapshot.cwd
        )
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
            raise ThreadSelectionError(
                f"No thread matches '{selector}'. Try /threads {selector}."
            )
        ranked.sort(key=lambda item: (item[0], item[1]))
        best_score = ranked[0][0]
        best_matches = [
            snapshot for score, _, snapshot in ranked if score == best_score
        ]
        if len(best_matches) > 1:
            labels = ", ".join(
                self._thread_short_label(snapshot) for snapshot in best_matches[:3]
            )
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
        threads = [
            self._remember_snapshot(item) for item in self._thread_list_items(result)
        ]
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
            snapshot = await self.read_thread(
                channel_id, conversation_id, binding.thread_id
            )
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
                raise AppServerError(
                    "thread list returned a repeated pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        threads = [threads_by_id[thread_id] for thread_id in thread_order]
        binding = self.store.get_binding(channel_id, conversation_id)
        if not search_term and binding.thread_id:
            seen_thread_ids = {snapshot.thread_id for snapshot in threads}
            if binding.thread_id not in seen_thread_ids:
                snapshot = await self.read_thread(
                    channel_id, conversation_id, binding.thread_id
                )
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
                    raise AppServerError(
                        "thread history returned a repeated pagination cursor"
                    )
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

    async def fork_thread(
        self, channel_id: str, conversation_id: str
    ) -> NativeThreadSnapshot:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        result = await self.client.fork_thread(thread_id)
        payload = result.get("thread")
        if not isinstance(payload, dict):
            forked_id = result.get("threadId")
            if forked_id is None:
                raise AppServerError("Codex did not return a forked thread")
            payload = {"id": forked_id}
        snapshot = self._remember_snapshot(payload)
        self.store.bind_thread_with_cwd(
            channel_id, conversation_id, snapshot.thread_id, snapshot.cwd
        )
        return snapshot

    async def rename_thread(
        self, channel_id: str, conversation_id: str, name: str
    ) -> dict:
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

    async def interrupt_active_turn(
        self, channel_id: str, conversation_id: str
    ) -> bool:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return False
        result = await self.client.resume_thread(
            thread_id=binding.thread_id,
            service_name=self.service_name,
        )
        payload = result.get("thread")
        if not isinstance(payload, dict):
            return False
        active = self._native_active_turn(payload)
        if active is None:
            return False
        return await self.interrupt_turn(binding.thread_id, active[0])

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> bool:
        try:
            await self.client.interrupt_turn(thread_id, turn_id)
        except AppServerError as exc:
            if not self._is_stale_turn_error(exc):
                raise
            return False
        return True

    def _remember_snapshot(self, payload: dict) -> NativeThreadSnapshot:
        status = self._native_status(payload.get("status"))
        thread_id = str(payload.get("id") or payload.get("threadId") or "")
        previous = self.store.get_thread_snapshot(thread_id)
        snapshot = NativeThreadSnapshot(
            thread_id=thread_id,
            cwd=str(
                payload.get("cwd")
                or (previous.cwd if previous is not None else "")
                or ""
            ),
            preview=str(
                payload.get("preview")
                or (previous.preview if previous is not None else "")
                or ""
            ),
            status=str(
                status
                or (previous.status if previous is not None else "idle")
                or "idle"
            ),
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
            if (
                turn_id
                and status is not None
                and status.strip().lower() in ACTIVE_THREAD_STATUSES
            ):
                return turn_id, status
        return None

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
        normalized = normalized.removeprefix("\\\\?\\")
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

    def _thread_match_score(
        self, snapshot: NativeThreadSnapshot, selector: str
    ) -> int | None:
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
        label = (
            snapshot.name
            or snapshot.preview
            or os.path.basename((snapshot.path or snapshot.cwd).rstrip("/\\"))
            or snapshot.thread_id
        )
        return label.strip() or snapshot.thread_id

    def _normalize_selector(self, value: str) -> str:
        lowered = value.strip().lower()
        lowered = lowered.replace("_", " ").replace("-", " ")
        lowered = re.sub(r"\s+", " ", lowered)
        return lowered

    def _min_score(self, current: int | None, candidate: int) -> int:
        return candidate if current is None else min(current, candidate)

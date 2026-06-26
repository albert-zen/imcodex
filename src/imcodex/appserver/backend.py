from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..models import NativeThreadSnapshot
from ..observability.runtime import emit_event
from ..store import ConversationStore
from .client import AppServerError


ACTIVE_THREAD_STATUSES = {"inprogress", "in_progress", "running", "working"}
PERMISSION_MODE_PROFILE_IDS = {
    "default": ":workspace",
    "read-only": ":read-only",
    "full-access": ":danger-full-access",
}
_LEGACY_PERMISSION_PRESETS = {
    "default": [
        {"keyPath": "approval_policy", "value": "on-request", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "workspace-write", "mergeStrategy": "replace"},
    ],
    "read-only": [
        {"keyPath": "approval_policy", "value": "on-request", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "read-only", "mergeStrategy": "replace"},
    ],
    "full-access": [
        {"keyPath": "approval_policy", "value": "never", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "danger-full-access", "mergeStrategy": "replace"},
    ],
}


class StaleThreadBindingError(RuntimeError):
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"thread binding is stale: {thread_id}")


class ThreadSelectionError(RuntimeError):
    pass


@dataclass(slots=True)
class TurnSubmission:
    kind: str
    thread_id: str
    turn_id: str


@dataclass(slots=True)
class ThreadListResult:
    threads: list[NativeThreadSnapshot]
    next_cursor: str | None = None


class CodexBackend:
    def __init__(self, *, client, store: ConversationStore, service_name: str) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name

    def prefers_native_recovery(self) -> bool:
        mode = getattr(self.client, "connection_mode", "") or getattr(self.client, "last_connection_mode", "")
        if mode == "disconnected":
            mode = getattr(self.client, "last_connection_mode", "")
        return mode in {"dedicated-ws", "shared-ws"}

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
                    personality="friendly",
                )
            except AppServerError as exc:
                if self._is_stale_thread_error(exc):
                    raise StaleThreadBindingError(binding.thread_id) from exc
                raise
            snapshot = self._remember_snapshot(result.get("thread") or {})
            self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
            return snapshot.thread_id
        if binding.bootstrap_cwd is None:
            raise KeyError("No working directory selected for thread session")
        result = await self.client.start_thread(
            cwd=binding.bootstrap_cwd,
            service_name=self.service_name,
            personality="friendly",
        )
        snapshot = self._remember_snapshot(result.get("thread") or {})
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
        return snapshot.thread_id

    async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
        result = await self.client.resume_thread(
            thread_id=thread_id,
            service_name=self.service_name,
            personality="friendly",
        )
        payload = result.get("thread")
        if not isinstance(payload, dict):
            raise AppServerError(f"thread {thread_id} is not available in Codex")
        snapshot = self._remember_snapshot(payload)
        self.store.bind_thread_with_cwd(channel_id, conversation_id, snapshot.thread_id, snapshot.cwd)
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
        return self._remember_snapshot(payload)

    async def read_thread_history(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        limit: int = 6,
    ) -> dict:
        thread_id = self._active_thread_id(channel_id, conversation_id)
        try:
            return await self.client.list_thread_turns(thread_id, limit=limit)
        except AppServerError as exc:
            if not self._is_unsupported_method_error(exc):
                raise
        return await self.client.read_thread(thread_id, include_turns=True)

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

    async def list_models(self) -> dict:
        return await self.client.list_models()

    async def read_account_rate_limits(self) -> dict:
        return await self.client.read_account_rate_limits()

    async def read_account_usage(self) -> dict:
        return await self.client.read_account_usage()

    async def read_account_credits(self) -> dict:
        result: dict = {}
        warnings: dict[str, str] = {}
        try:
            result["rateLimitsResult"] = await self.read_account_rate_limits()
        except AppServerError as exc:
            warnings["rateLimits"] = str(exc)
        try:
            result["usageResult"] = await self.read_account_usage()
        except AppServerError as exc:
            warnings["usage"] = str(exc)
        if warnings:
            result["warnings"] = warnings
        if "rateLimitsResult" not in result and "usageResult" not in result:
            raise AppServerError("account rate limits and usage are unavailable")
        return result

    async def read_permission_options(self, channel_id: str, conversation_id: str) -> dict:
        result = await self.read_config(channel_id, conversation_id)
        warnings: dict[str, str] = {}
        try:
            result["profiles"] = await self._list_permission_profiles(channel_id, conversation_id)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            warnings["profiles"] = str(exc)
            result["profiles"] = []
            result["nativeProfilesSupported"] = False
        else:
            result["nativeProfilesSupported"] = True
        try:
            result.update(await self.client.read_config_requirements())
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            warnings["requirements"] = str(exc)
        if warnings:
            result["warnings"] = warnings
        return result

    async def set_permission_mode(self, channel_id: str, conversation_id: str, mode: str) -> dict:
        profile_id = PERMISSION_MODE_PROFILE_IDS.get(mode)
        if profile_id is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        try:
            options = await self.read_permission_options(channel_id, conversation_id)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            return await self._set_legacy_permission_mode(mode, warning=str(exc))
        if options.get("nativeProfilesSupported") is False:
            warning = str((options.get("warnings") or {}).get("profiles") or "")
            return await self._set_legacy_permission_mode(mode, warning=warning)
        if not self._permission_profile_is_available(profile_id, options):
            raise AppServerError(f"permission profile {profile_id} is not available in Codex")
        if not self._permission_profile_is_allowed(profile_id, options.get("requirements")):
            raise AppServerError(f"permission profile {profile_id} is not allowed by Codex requirements")
        write_result = await self.write_config_value(
            key_path="default_permissions",
            value=profile_id,
            merge_strategy="replace",
        )
        write_result["mode"] = mode
        write_result["profile"] = profile_id
        write_result["fallback"] = False
        return write_result

    async def read_config(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        include_layers: bool = False,
    ) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self.client.read_config(include_layers=include_layers, cwd=cwd)

    async def write_config_value(
        self,
        *,
        key_path: str,
        value: object,
        merge_strategy: str = "replace",
    ) -> dict:
        return await self.client.write_config_value(
            key_path=key_path,
            value=value,
            merge_strategy=merge_strategy,
        )

    async def batch_write_config(
        self,
        *,
        edits: list[dict],
        reload_user_config: bool = False,
    ) -> dict:
        return await self.client.batch_write_config(
            edits=edits,
            reload_user_config=reload_user_config,
        )

    async def set_default_model(self, model: str | None) -> dict:
        return await self.write_config_value(key_path="model", value=model, merge_strategy="replace")

    async def call_native(self, method: str, params: dict | None = None) -> dict:
        return await self.client.call(method, params)

    async def submit_text(self, channel_id: str, conversation_id: str, text: str) -> TurnSubmission:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is not None:
            active = self.store.get_active_turn(binding.thread_id)
            if active is not None and active[1] == "inProgress":
                try:
                    await self.client.steer_turn(binding.thread_id, active[0], text)
                except AppServerError as exc:
                    if not self._is_stale_turn_error(exc):
                        raise
                    self.store.clear_active_turn(binding.thread_id)
                else:
                    return TurnSubmission(kind="steer", thread_id=binding.thread_id, turn_id=active[0])
            try:
                return await self._start_turn(binding.thread_id, text)
            except AppServerError as exc:
                if not self._requires_thread_resume(exc):
                    raise
        thread_id = await self.ensure_thread(channel_id, conversation_id)
        return await self._start_turn(thread_id, text)

    async def _start_turn(self, thread_id: str, text: str) -> TurnSubmission:
        result = await self.client.start_turn(thread_id=thread_id, text=text, summary="concise")
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
            self.store.clear_active_turn(thread_id)
            self.store.remove_pending_requests_for_turn(thread_id, turn_id)
            return False
        self.store.suppress_turn(thread_id, turn_id)
        self.store.clear_active_turn(thread_id)
        self.store.remove_pending_requests_for_turn(thread_id, turn_id)
        return True

    async def rehydrate_bound_threads(self) -> None:
        for binding in self.store.iter_bindings():
            if not binding.thread_id:
                continue
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
                result = await self.client.resume_thread(
                    thread_id=binding.thread_id,
                    service_name=self.service_name,
                    personality="friendly",
                )
            except AppServerError as exc:
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.failed",
                    level="WARNING",
                    message=str(exc),
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": binding.thread_id,
                        "error_type": type(exc).__name__,
                    },
                )
                if self._is_stale_thread_error(exc):
                    self.store.clear_thread_binding(binding.channel_id, binding.conversation_id)
                continue
            payload = result.get("thread")
            if not isinstance(payload, dict):
                emit_event(
                    component="appserver.backend",
                    event="bridge.thread_rehydrate.empty",
                    level="WARNING",
                    message="Thread resume returned no thread payload",
                    data={
                        "channel_id": binding.channel_id,
                        "conversation_id": binding.conversation_id,
                        "thread_id": binding.thread_id,
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
            active = self.store.get_active_turn(snapshot.thread_id)
            if active is not None and snapshot.status.strip().lower() not in ACTIVE_THREAD_STATUSES:
                self.store.suppress_turn(snapshot.thread_id, active[0])
                self.store.clear_active_turn(snapshot.thread_id)
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

    async def reply_to_server_request(self, request_id: str, decision_or_answers: dict) -> None:
        route = self.store.get_pending_request(request_id)
        if route is None or route.transport_request_id is None:
            raise AppServerError(f"unknown pending request: {request_id}")
        await self.client.reply_to_transport_request(route.transport_request_id, decision_or_answers)
        self.store.remove_pending_request(request_id)

    async def reply_error_to_server_request(
        self,
        request_id: str,
        *,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        route = self.store.get_pending_request(request_id)
        if route is None or route.transport_request_id is None:
            raise AppServerError(f"unknown pending request: {request_id}")
        await self.client.reply_error_to_transport_request(
            route.transport_request_id,
            code=code,
            message=message,
            data=data,
        )
        self.store.remove_pending_request(request_id)

    async def reply_error_to_transport_request(
        self,
        transport_request_id: str | int,
        *,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        await self.client.reply_error_to_transport_request(
            transport_request_id,
            code=code,
            message=message,
            data=data,
        )

    async def _list_permission_profiles(self, channel_id: str, conversation_id: str) -> list[dict]:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        cursor: str | None = None
        profiles: list[dict] = []
        while True:
            params: dict[str, object] = {}
            if cwd is not None:
                params["cwd"] = cwd
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.client.list_permission_profiles(params)
            profiles.extend(item for item in result.get("data", []) if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return profiles
            cursor = str(next_cursor)

    async def _set_legacy_permission_mode(self, mode: str, *, warning: str = "") -> dict:
        edits = _LEGACY_PERMISSION_PRESETS.get(mode)
        if edits is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        result = await self.batch_write_config(edits=edits, reload_user_config=False)
        result["mode"] = mode
        result["fallback"] = True
        if warning:
            result["warning"] = warning
        return result

    def _permission_profile_is_available(self, profile_id: str, options: dict) -> bool:
        profiles = options.get("profiles")
        if not isinstance(profiles, list):
            return False
        return any(isinstance(profile, dict) and profile.get("id") == profile_id for profile in profiles)

    def _permission_profile_is_allowed(self, profile_id: str, requirements: object) -> bool:
        if not isinstance(requirements, dict):
            return True
        allowed = requirements.get("allowedPermissionProfiles")
        if not isinstance(allowed, dict):
            return True
        return bool(allowed.get(profile_id))

    def _is_native_permission_profile_unsupported(self, error: AppServerError) -> bool:
        return self._is_unsupported_method_error(error)

    def _is_unsupported_method_error(self, error: AppServerError) -> bool:
        if getattr(error, "code", None) == -32601:
            return True
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "method not found",
                "unknown method",
                "not implemented",
                "unsupported method",
                "no handler",
            )
        )

    def _remember_snapshot(self, payload: dict) -> NativeThreadSnapshot:
        status = payload.get("status")
        if isinstance(status, dict):
            status = status.get("type") or status.get("status")
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

    def _is_stale_thread_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "invalid request",
                "not found",
                "unknown thread",
                "no such thread",
                "no rollout found",
            )
        )

    def _requires_thread_resume(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return self._is_stale_thread_error(error) or any(
            marker in message
            for marker in (
                "not loaded",
                "must resume",
                "thread closed",
            )
        )

    def _is_stale_turn_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return self._is_stale_thread_error(error) or any(
            marker in message
            for marker in (
                "no active turn",
                "unknown turn",
                "no such turn",
                "turn not found",
                "expected turn",
            )
        )

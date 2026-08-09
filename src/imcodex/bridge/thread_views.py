from __future__ import annotations

import asyncio
import ntpath
import posixpath
import time
from pathlib import Path

from ..appserver import AppServerError
from ..models import InboundMessage, NativeThreadSnapshot
from .settings import (
    current_model_label,
    current_reasoning_label,
    effective_config,
    fast_mode_label,
    permission_mode_label,
)


_THREADS_PAGE_SIZE = 10
_INLINE_PROJECT_LIMIT = 8
_THREAD_BROWSER_TITLE_LIMIT = 72
_STATUS_QUERY_TIMEOUT_S = 2.5


class ThreadViewMixin:
    async def _render_threads(
        self,
        message: InboundMessage,
        *,
        page: int = 1,
        query: str | None = None,
        project: str | None = None,
        refresh: bool = True,
        catalog: list[NativeThreadSnapshot] | None = None,
    ) -> str:
        threads = (
            list(catalog)
            if catalog is not None
            else await self._load_thread_catalog(message, query=query, refresh=refresh)
        )
        project_paths = await asyncio.to_thread(self._thread_project_paths, threads)
        project_options = self._thread_project_options(threads, project_paths=project_paths)
        project_path, project_error = self._resolve_thread_project(project, project_options)
        if project_error is not None:
            self.store.set_thread_browser_context(
                message.channel_id,
                message.conversation_id,
                thread_ids=[],
                page=1,
                total=1,
                query=query,
                all_thread_ids=[snapshot.thread_id for snapshot in threads],
                project_paths=[path for path, _label in project_options],
                project_path=None,
            )
            return "\n".join(
                [
                    project_error,
                    self._thread_project_choices(project_options),
                    "Use /threads --project <name-or-number> to choose a project.",
                ]
            )

        filtered = self._filter_threads_by_project(
            threads,
            project_path,
            project_paths=project_paths,
        )
        page_count = max(1, (len(filtered) + _THREADS_PAGE_SIZE - 1) // _THREADS_PAGE_SIZE)
        safe_page = min(max(page, 1), page_count)
        page_start = (safe_page - 1) * _THREADS_PAGE_SIZE
        visible = filtered[page_start : page_start + _THREADS_PAGE_SIZE]
        self.store.set_thread_browser_context(
            message.channel_id,
            message.conversation_id,
            thread_ids=[snapshot.thread_id for snapshot in visible],
            page=safe_page,
            total=page_count,
            query=query,
            all_thread_ids=[snapshot.thread_id for snapshot in threads],
            project_paths=[path for path, _label in project_options],
            project_path=project_path,
        )
        project_label = self._selected_project_label(project_path, project_options)
        lines = [self._thread_page_heading(safe_page, page_count, project_label=project_label)]
        if not visible:
            lines.append("(none)")
        for index, snapshot in enumerate(visible, start=1):
            is_current = (
                snapshot.thread_id
                == self.store.get_binding(message.channel_id, message.conversation_id).thread_id
            )
            current_marker = " ✓" if is_current else ""
            pin_marker = "📌 " if snapshot.pinned is True else ""
            identity = self._thread_identity_label(
                snapshot,
                project_options=project_options,
                project_path=project_paths.get(snapshot.thread_id),
            )
            lines.append(
                f"{index}. {pin_marker}{self._thread_browser_title(snapshot)} "
                f"[{identity}]{current_marker}"
            )
        if threads and not any(snapshot.pinned is not None for snapshot in threads):
            lines.append("Pinned status is unavailable from this Codex App Server.")
        if project_options:
            lines.append(self._thread_project_choices(project_options, selected_path=project_path))
        actions = ["Use /pick <n> to switch", "/new to start fresh", "/exit to close"]
        if safe_page < page_count:
            actions.insert(1, "/next for more")
        if safe_page > 1:
            actions.insert(1, "/prev for previous")
        if query is None:
            actions.append("/threads <keyword> to filter")
        if project_options:
            actions.append("/threads --project <name-or-number> to filter by project")
        return "\n".join(lines + ["; ".join(actions) + "."])

    async def _load_thread_catalog(
        self,
        message: InboundMessage,
        *,
        query: str | None,
        refresh: bool,
    ) -> list[NativeThreadSnapshot]:
        context = self.store.get_thread_browser_context(message.channel_id, message.conversation_id)
        if not refresh and context is not None and context.query == query:
            cached = [
                self.store.get_thread_snapshot(thread_id)
                for thread_id in context.all_thread_ids
            ]
            if all(snapshot is not None for snapshot in cached):
                return [snapshot for snapshot in cached if snapshot is not None]
        result = await self.backend.query_all_threads(
            message.channel_id,
            message.conversation_id,
            search_term=query,
        )
        return result.threads

    def _thread_project_options(
        self,
        threads: list[NativeThreadSnapshot],
        *,
        project_paths: dict[str, str | None] | None = None,
    ) -> list[tuple[str, str]]:
        paths: list[str] = []
        seen: set[str] = set()
        for snapshot in threads:
            path = (
                project_paths.get(snapshot.thread_id)
                if project_paths is not None
                else self._thread_project_path(snapshot)
            )
            if path is None:
                continue
            key = self._normalized_project_path(path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
        return list(zip(paths, self._unique_project_labels(paths), strict=True))

    def _resolve_thread_project(
        self,
        selector: str | None,
        options: list[tuple[str, str]],
    ) -> tuple[str | None, str | None]:
        if selector is None or selector.casefold() in {"0", "all"}:
            return None, None
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(options):
                return options[index][0], None
            return None, f"Unknown project number: {selector}."
        normalized = self._normalized_project_path(selector)
        path_matches = [path for path, _label in options if self._normalized_project_path(path) == normalized]
        if len(path_matches) == 1:
            return path_matches[0], None
        label_matches = [path for path, label in options if label.casefold() == selector.casefold()]
        if len(label_matches) == 1:
            return label_matches[0], None
        if len(label_matches) > 1:
            return None, f"Project name '{selector}' is ambiguous; choose its number instead."
        return None, f"Unknown project: {selector}."

    def _filter_threads_by_project(
        self,
        threads: list[NativeThreadSnapshot],
        project_path: str | None,
        *,
        project_paths: dict[str, str | None] | None = None,
    ) -> list[NativeThreadSnapshot]:
        if project_path is None:
            return threads
        selected = self._normalized_project_path(project_path)
        return [
            snapshot
            for snapshot in threads
            if (
                path := (
                    project_paths.get(snapshot.thread_id)
                    if project_paths is not None
                    else self._thread_project_path(snapshot)
                )
            ) is not None
            and self._normalized_project_path(path) == selected
        ]

    def _thread_project_paths(
        self,
        threads: list[NativeThreadSnapshot],
    ) -> dict[str, str | None]:
        """Resolve display-only project roots once per browser render.

        Native cwd/path remains unchanged for execution and handoff. Filesystem
        probing runs off the event loop; pointer reads are size-bounded and
        repeated native paths are resolved only once per render.
        """

        canonical_by_raw: dict[str, str] = {}
        result: dict[str, str | None] = {}
        for snapshot in threads:
            raw = self._thread_native_project_path(snapshot)
            if raw is None:
                result[snapshot.thread_id] = None
                continue
            raw_key = self._normalized_project_path(raw)
            canonical = canonical_by_raw.get(raw_key)
            if canonical is None:
                canonical = self._git_project_root(raw) or raw
                canonical_by_raw[raw_key] = canonical
            result[snapshot.thread_id] = canonical
        return result

    def _thread_project_path(self, snapshot: NativeThreadSnapshot) -> str | None:
        raw = self._thread_native_project_path(snapshot)
        if raw is None:
            return None
        return self._git_project_root(raw) or raw

    def _thread_native_project_path(self, snapshot: NativeThreadSnapshot) -> str | None:
        for candidate in (snapshot.cwd, snapshot.path):
            text = str(candidate or "").strip()
            if text:
                return text
        return None

    def _git_project_root(self, path: str) -> str | None:
        # Foreign path syntaxes and deleted worktrees safely retain their native
        # display path. No git subprocess or Desktop-private database is used.
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or not candidate.exists():
            return None
        if candidate.is_file():
            candidate = candidate.parent
        for directory in (candidate, *candidate.parents):
            marker = directory / ".git"
            if marker.is_dir():
                return str(directory)
            if not marker.is_file():
                continue
            git_dir = self._read_git_pointer(marker, prefix="gitdir:")
            if git_dir is None:
                return str(directory)
            common_pointer = git_dir / "commondir"
            common_dir = self._read_git_pointer(common_pointer)
            if common_dir is not None and common_dir.name == ".git":
                return str(common_dir.parent)
            return str(directory)
        return None

    def _read_git_pointer(self, path: Path, *, prefix: str | None = None) -> Path | None:
        try:
            if not path.is_file() or path.stat().st_size > 4096:
                return None
            value = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, UnicodeError, IndexError):
            return None
        if prefix is not None:
            if not value.casefold().startswith(prefix.casefold()):
                return None
            value = value[len(prefix) :].strip()
        if not value:
            return None
        pointer = Path(value)
        if not pointer.is_absolute():
            pointer = path.parent / pointer
        try:
            return pointer.resolve(strict=True)
        except OSError:
            return None

    def _unique_project_labels(self, paths: list[str]) -> list[str]:
        parts = [self._path_parts(path) for path in paths]
        labels = [items[-1] if items else path for path, items in zip(paths, parts, strict=True)]
        duplicate_groups: dict[str, list[int]] = {}
        for index, label in enumerate(labels):
            duplicate_groups.setdefault(label.casefold(), []).append(index)
        for indices in duplicate_groups.values():
            if len(indices) < 2:
                continue
            max_depth = max(len(parts[index]) for index in indices)
            for depth in range(2, max_depth + 1):
                candidates = {
                    index: "/".join(parts[index][-depth:])
                    for index in indices
                }
                if len({value.casefold() for value in candidates.values()}) == len(indices):
                    for index, value in candidates.items():
                        labels[index] = value
                    break
            else:
                for index in indices:
                    labels[index] = paths[index]
        return labels

    def _path_parts(self, path: str) -> list[str]:
        if ntpath.splitdrive(path)[0] or "\\" in path:
            drive, tail = ntpath.splitdrive(path.replace("/", "\\"))
            return [item for item in ([drive.rstrip("\\")] + tail.split("\\")) if item]
        return [item for item in posixpath.normpath(path).split("/") if item]

    def _normalized_project_path(self, path: str) -> str:
        normalized = path.strip()
        if normalized.casefold().startswith("\\\\?\\unc\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        if ntpath.splitdrive(normalized)[0] or "\\" in normalized:
            windows_path = normalized.replace("/", "\\")
            return "windows:" + ntpath.normcase(ntpath.normpath(windows_path))
        return "posix:" + posixpath.normpath(normalized)

    def _path_leaf(self, path: str) -> str:
        text = path.rstrip("/\\")
        return text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or path

    def _selected_project_label(
        self,
        project_path: str | None,
        options: list[tuple[str, str]],
    ) -> str:
        if project_path is None:
            return "All projects"
        selected = self._normalized_project_path(project_path)
        return next(
            (label for path, label in options if self._normalized_project_path(path) == selected),
            self._path_leaf(project_path),
        )

    def _thread_project_choices(
        self,
        options: list[tuple[str, str]],
        *,
        selected_path: str | None = None,
    ) -> str:
        choices = ["[0] All"]
        visible_indices = list(range(min(len(options), _INLINE_PROJECT_LIMIT)))
        if selected_path is not None:
            selected = self._normalized_project_path(selected_path)
            selected_index = next(
                (
                    index
                    for index, (path, _label) in enumerate(options)
                    if self._normalized_project_path(path) == selected
                ),
                None,
            )
            if selected_index is not None and selected_index not in visible_indices:
                visible_indices[-1:] = [selected_index]
        choices.extend(
            f"[{index + 1}] {options[index][1]}"
            for index in visible_indices
        )
        hidden_count = len(options) - len(visible_indices)
        if hidden_count > 0:
            choices.append(f"… +{hidden_count} more")
        return "Projects: " + " · ".join(choices)

    def _thread_page_heading(self, page: int, page_count: int, *, project_label: str) -> str:
        return f"Threads · [{project_label}] · Page {page}/{page_count}"

    async def _render_status(self, message: InboundMessage) -> str:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        cwd = self.store.current_cwd(message.channel_id, message.conversation_id) or "(none)"
        config = await self._read_status_config(message.channel_id, message.conversation_id)
        current_config = effective_config(config)
        if binding.thread_id is None:
            thread_label = "(none)"
            state = "Idle"
        else:
            try:
                snapshot = await asyncio.wait_for(
                    self.backend.read_thread(
                        message.channel_id,
                        message.conversation_id,
                        binding.thread_id,
                    ),
                    timeout=_STATUS_QUERY_TIMEOUT_S,
                )
            except (asyncio.TimeoutError, AppServerError):
                snapshot = None
            if snapshot is None:
                thread_label = binding.thread_id
                state = "Unavailable"
            else:
                thread_label = self._thread_label(snapshot)
                cwd = snapshot.cwd or cwd
                active = self.store.get_active_turn(binding.thread_id)
                state = "Working" if active and active[1] == "inProgress" else self._human_state(snapshot.status)
        app_server = self.backend.app_server_connection_facts()
        fast_label = fast_mode_label(
            current_config,
            default_service_tier=config.get("selectedModelDefaultServiceTier"),
            feature_available=config.get("fastAvailable") is not False,
            fast_supported=(
                config.get("fastSupported") if isinstance(config.get("fastSupported"), bool) else None
            ),
        )
        return "\n".join(
            [
                "Status",
                "",
                f"CWD: {cwd}",
                f"Thread: {thread_label}",
                f"State: {state}",
                f"App Server: {self._app_server_status_label(app_server)}",
                f"Ownership: {self._app_server_ownership_label(app_server)}",
                f"Transport: {self._app_server_transport_label(app_server)}",
                f"Endpoint: {app_server.get('endpoint') or '(unknown)'}",
                f"Connection epoch: {int(app_server.get('connection_epoch') or 0)}",
                f"Model: {current_model_label(current_config)}",
                f"Reasoning: {current_reasoning_label(current_config)}",
                f"Fast mode: {fast_label}",
                f"Permissions: {permission_mode_label(current_config)}",
                f"Bridge visibility: {self._bridge_visibility_label(binding)}",
                f"Pending approvals: {len(self.store.list_pending_requests(message.channel_id, message.conversation_id, kind='approval'))}",
            ]
        )

    async def _render_thread(self, message: InboundMessage, thread_id: str | None) -> str:
        if thread_id is None:
            return "No active thread."
        try:
            snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, thread_id)
        except AppServerError as exc:
            return f"Current thread {thread_id} could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
        if snapshot is None:
            return f"Current thread {thread_id} is no longer available in Codex."
        return "\n".join(
            [
                f"Thread: {self._thread_label(snapshot)}",
                f"Thread id: {snapshot.thread_id}",
                f"Workspace: {self._thread_workspace_label(snapshot)}",
                f"CWD: {snapshot.cwd or '(unknown)'}",
                f"Path: {snapshot.path or snapshot.cwd or '(unknown)'}",
                f"Status: {snapshot.status}",
                f"Source: {snapshot.source or 'unknown'}",
            ]
        )

    def _thread_label(self, snapshot) -> str:
        for candidate in (snapshot.name, snapshot.preview, snapshot.path, snapshot.cwd):
            if not candidate:
                continue
            text = str(candidate).strip()
            if text:
                if "\\" in text or "/" in text:
                    text = text.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                return text
        return snapshot.thread_id

    def _thread_workspace_label(self, snapshot) -> str:
        for candidate in (snapshot.cwd, snapshot.path):
            if not candidate:
                continue
            text = str(candidate).strip()
            if not text:
                continue
            if "\\" in text or "/" in text:
                text = text.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            return text or "(unknown)"
        return "(unknown)"

    def _thread_browser_title(self, snapshot: NativeThreadSnapshot) -> str:
        title = " ".join(self._thread_label(snapshot).split())
        if len(title) <= _THREAD_BROWSER_TITLE_LIMIT:
            return title
        return title[: _THREAD_BROWSER_TITLE_LIMIT - 1].rstrip() + "…"

    def _thread_identity_label(
        self,
        snapshot: NativeThreadSnapshot,
        *,
        project_options: list[tuple[str, str]],
        project_path: str | None,
    ) -> str:
        project_label = "Unknown project"
        if project_path is not None:
            normalized = self._normalized_project_path(project_path)
            project_label = next(
                (
                    label
                    for option_path, label in project_options
                    if self._normalized_project_path(option_path) == normalized
                ),
                self._path_leaf(project_path),
            )
        details = [project_label]
        status = self._thread_status_label(snapshot.status)
        if status is not None:
            details.append(status)
        age = self._thread_age_label(snapshot.updated_at)
        if age is not None:
            details.append(age)
        return " · ".join(details)

    def _thread_status_label(self, status: str | None) -> str | None:
        normalized = str(status or "").strip().replace("-", "").replace("_", "").casefold()
        if normalized in {"active", "inprogress", "running", "working"}:
            return "Working"
        if normalized in {"systemerror", "failed"}:
            return "Error"
        if normalized in {"idle", "completed", "interrupted"}:
            return "Idle"
        return None

    def _thread_age_label(self, updated_at: float | None, *, now: float | None = None) -> str | None:
        if updated_at is None:
            return None
        current = time.time() if now is None else now
        age = max(0, int(current - updated_at))
        if age < 60:
            return "now"
        if age < 3600:
            return f"{age // 60}m ago"
        if age < 86400:
            return f"{age // 3600}h ago"
        return f"{age // 86400}d ago"

    def _bridge_visibility_label(self, binding) -> str:
        return binding.visibility_profile.replace("-", " ").title()

    def _app_server_status_label(self, facts: dict) -> str:
        labels = {
            "connected": "Connected",
            "initializing": "Initializing",
            "reconnecting": "Reconnecting",
            "disconnected": "Disconnected",
        }
        status = str(facts.get("status") or "disconnected")
        return labels.get(status, status.replace("-", " ").title())

    def _app_server_ownership_label(self, facts: dict) -> str:
        if facts.get("ownership") == "external":
            return "Externally managed"
        if facts.get("ownership") == "bridge-child":
            return "Bridge child (compatibility)"
        return "Unknown"

    def _app_server_transport_label(self, facts: dict) -> str:
        labels = {
            "unix-websocket": "Unix WebSocket",
            "tcp-websocket": "TCP WebSocket",
            "stdio-jsonl": "stdio JSONL",
        }
        transport = str(facts.get("transport") or "unknown")
        return labels.get(transport, transport.replace("-", " ").title())

    def _human_state(self, status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"inprogress", "in_progress", "working", "running"}:
            return "Working"
        if normalized == "completed":
            return "Completed"
        if normalized == "failed":
            return "Failed"
        return "Idle" if normalized == "idle" else str(status or "Idle").title()

    async def _read_status_config(self, channel_id: str, conversation_id: str) -> dict:
        try:
            base = await asyncio.wait_for(
                self.backend.read_effective_settings(
                    channel_id,
                    conversation_id,
                    include_model_metadata=False,
                ),
                timeout=_STATUS_QUERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, AppServerError):
            return {"config": {}}
        try:
            return await asyncio.wait_for(
                self.backend.enrich_effective_settings(base),
                timeout=_STATUS_QUERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, AppServerError):
            return base

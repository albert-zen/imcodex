from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .context import InstanceContext
from .events import EventWriter
from .health import HealthWriter
from .logger import configure_observability_logging, reset_observability_logging
from .paths import ObservabilityPaths

_ACTIVE_RUNTIME: "ObservabilityRuntime | None" = None


class ObservabilityRuntime:
    def __init__(
        self,
        *,
        run_root: Path,
        service_name: str,
        log_level: str,
        http_host: str,
        http_port: int,
        app_server_url: str | None,
        cwd: Path,
        clock: Callable[[], datetime] | None = None,
        pid_provider: Callable[[], int] | None = None,
        git_metadata_provider: Callable[[Path], dict[str, str]] | None = None,
        retention: int = 20,
    ) -> None:
        self.run_root = Path(run_root)
        self.service_name = service_name
        self.log_level = log_level
        self.http_host = http_host
        self.http_port = http_port
        self.app_server_url = app_server_url
        self.cwd = Path(cwd)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pid_provider = pid_provider or os.getpid
        self.git_metadata_provider = git_metadata_provider or _read_git_metadata
        self.retention = retention
        self.instance_id = ""
        self.context: InstanceContext | None = None
        self.paths: ObservabilityPaths | None = None
        self.event_writer: EventWriter | None = None
        self.health_writer: HealthWriter | None = None
        self._pending_launch_snapshot: dict[str, Any] | None = None

    def start(self) -> None:
        started_at = self.clock()
        pid = self.pid_provider()
        self.instance_id = f"{started_at.strftime('%Y%m%d-%H%M%S')}-p{pid}"
        self.paths = ObservabilityPaths.build(self.run_root, self.instance_id)
        self._prepare_directories()

        git_metadata = self.git_metadata_provider(self.cwd)
        self.context = InstanceContext(
            instance_id=self.instance_id,
            pid=pid,
            started_at=started_at.astimezone().isoformat(),
            service_name=self.service_name,
            cwd=str(self.cwd),
            git_branch=git_metadata.get("git_branch", "unknown"),
            git_commit=git_metadata.get("git_commit", "unknown"),
            python_version=platform.python_version(),
            http_host=self.http_host,
            http_port=self.http_port,
            app_server_url=self.app_server_url,
        )
        self._write_instance_metadata()
        configure_observability_logging(
            level=self.log_level,
            instance_id=self.instance_id,
            log_paths=[self.paths.log_path, self.paths.current_log_path],
        )
        self.event_writer = EventWriter(paths=self.paths, context=self.context, clock=self.clock)
        self.health_writer = HealthWriter(paths=self.paths, context=self.context, clock=self.clock)
        if self._pending_launch_snapshot is not None:
            self._persist_launch_snapshot(self._pending_launch_snapshot)
        self._prune_archived_runs()
        set_active_runtime(self)

    def stop(self) -> None:
        if self.event_writer is not None:
            self.event_writer.close()
        if self.health_writer is not None:
            self.health_writer.update(status="stopped")
        reset_observability_logging()
        clear_active_runtime()

    def emit_event(
        self,
        *,
        component: str,
        event: str,
        level: str = "INFO",
        message: str = "",
        data: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        if self.event_writer is None:
            return
        self.event_writer.emit(
            component=component,
            event=event,
            level=level,
            message=message,
            data=data,
            **fields,
        )

    def update_health(self, **changes: Any) -> None:
        if self.health_writer is None:
            return
        self.health_writer.update(**changes)

    def mark_channel_health(self, channel_id: str, **changes: Any) -> None:
        if self.health_writer is None:
            return
        self.health_writer.merge_channel(channel_id, **changes)

    def mark_http_health(self, **changes: Any) -> None:
        if self.health_writer is None:
            return
        self.health_writer.merge_http(**changes)

    def mark_appserver_health(self, **changes: Any) -> None:
        if self.health_writer is None:
            return
        self.health_writer.merge_appserver(**changes)

    def _prepare_directories(self) -> None:
        assert self.paths is not None
        self.paths.runs_dir.mkdir(parents=True, exist_ok=True)
        self.paths.instance_dir.mkdir(parents=True, exist_ok=True)
        if self.paths.current_dir.exists():
            shutil.rmtree(self.paths.current_dir)
        self.paths.current_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.paths.log_path, self.paths.current_log_path):
            path.touch(exist_ok=True)

    def _write_instance_metadata(self) -> None:
        assert self.paths is not None
        assert self.context is not None
        payload = json.dumps(self.context.to_dict(), ensure_ascii=True, indent=2)
        self.paths.instance_metadata_path.write_text(payload + "\n", encoding="utf-8")
        self.paths.current_metadata_path.write_text(payload + "\n", encoding="utf-8")

    def write_launch_snapshot(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        payload = {
            "command": command,
            "cwd": str(cwd),
            "env": env,
            "pid": self.pid_provider(),
            "port": self.http_port,
        }
        if self.paths is None:
            self._pending_launch_snapshot = payload
            return
        self._persist_launch_snapshot(payload)

    def _persist_launch_snapshot(self, payload: dict[str, Any]) -> None:
        assert self.paths is not None
        serialized = json.dumps(payload, ensure_ascii=True, indent=2)
        self.paths.launch_path.write_text(serialized + "\n", encoding="utf-8")
        self.paths.current_launch_path.write_text(serialized + "\n", encoding="utf-8")
        self._pending_launch_snapshot = payload

    def _prune_archived_runs(self) -> None:
        assert self.paths is not None
        if self.retention <= 0:
            return
        directories = sorted(
            [path for path in self.paths.runs_dir.iterdir() if path.is_dir()],
            key=lambda path: path.name,
        )
        overflow = len(directories) - self.retention
        for path in directories[: max(0, overflow)]:
            shutil.rmtree(path, ignore_errors=True)


def set_active_runtime(runtime: ObservabilityRuntime) -> None:
    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = runtime


def get_active_runtime() -> ObservabilityRuntime | None:
    return _ACTIVE_RUNTIME


def clear_active_runtime() -> None:
    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = None


def emit_event(
    *,
    component: str,
    event: str,
    level: str = "INFO",
    message: str = "",
    data: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    runtime = get_active_runtime()
    if runtime is not None:
        runtime.emit_event(
            component=component,
            event=event,
            level=level,
            message=message,
            data=data,
            **fields,
        )


def update_health(**changes: Any) -> None:
    runtime = get_active_runtime()
    if runtime is not None:
        runtime.update_health(**changes)


def mark_channel_health(channel_id: str, **changes: Any) -> None:
    runtime = get_active_runtime()
    if runtime is not None:
        runtime.mark_channel_health(channel_id, **changes)


def mark_http_health(**changes: Any) -> None:
    runtime = get_active_runtime()
    if runtime is not None:
        runtime.mark_http_health(**changes)


def mark_appserver_health(**changes: Any) -> None:
    runtime = get_active_runtime()
    if runtime is not None:
        runtime.mark_appserver_health(**changes)


def _read_git_metadata(cwd: Path) -> dict[str, str]:
    return {
        "git_branch": _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "git_commit": _run_git(cwd, "rev-parse", "--short", "HEAD") or "unknown",
    }


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None

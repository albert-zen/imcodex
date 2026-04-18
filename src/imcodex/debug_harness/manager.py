from __future__ import annotations

import json
import os
import socket
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from .models import DebugRunManifest
from .paths import DebugHarnessPaths


Launcher = Callable[..., object]


class DebugInstanceManager:
    def __init__(
        self,
        *,
        root: Path,
        repo_root: Path,
        launcher: Launcher | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.repo_root = Path(repo_root)
        self.launcher = launcher or self._default_launcher
        self.now = now or self._default_now
        self._active_processes: dict[str, object] = {}

    def start(
        self,
        *,
        port: int,
        run_id: str | None = None,
        purpose: str | None = None,
        qq_enabled: bool = False,
        app_server_url: str | None = None,
    ) -> DebugRunManifest:
        run_id = run_id or self._next_run_id()
        paths = DebugHarnessPaths.build(self.root, run_id)
        self._prepare_layout(paths, purpose=purpose)

        env = os.environ.copy()
        env.update(
            {
                "IMCODEX_HTTP_PORT": str(port),
                "IMCODEX_DATA_DIR": str(paths.data_path),
                "IMCODEX_RUN_DIR": str(paths.observability_run_path),
                "IMCODEX_QQ_ENABLED": "1" if qq_enabled else "0",
                "IMCODEX_DEBUG_API_ENABLED": "1",
            }
        )
        if app_server_url is not None:
            env["IMCODEX_APP_SERVER_URL"] = app_server_url

        process = self.launcher(command=["python", "-m", "imcodex"], cwd=self.repo_root, env=env)
        manifest = DebugRunManifest(
            run_id=run_id,
            pid=int(getattr(process, "pid")),
            port=port,
            purpose=purpose,
            cwd=str(paths.cwd_path),
            data_dir=str(paths.data_path),
            run_dir=str(paths.observability_run_path),
            started_at=self.now(),
            status="running",
        )
        self._write_manifest(paths.manifest_path, manifest)
        self._active_processes[run_id] = process
        return manifest

    def stop(self, run_id: str) -> DebugRunManifest:
        manifest = self._read_manifest(run_id)
        process = self._active_processes.pop(run_id, None)
        if process is not None:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()
            wait = getattr(process, "wait", None)
            if callable(wait):
                wait(timeout=10)
        else:
            self._terminate_pid(manifest.pid)
        manifest.status = "stopped"
        self._write_manifest(DebugHarnessPaths.build(self.root, run_id).manifest_path, manifest)
        return manifest

    def list_runs(self) -> list[DebugRunManifest]:
        manifests_dir = self.root / "manifests"
        if not manifests_dir.exists():
            return []
        manifests = [
            DebugRunManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in manifests_dir.glob("*.json")
        ]
        return sorted(manifests, key=lambda item: item.run_id)

    def get_run(self, run_id: str) -> DebugRunManifest:
        return self._read_manifest(run_id)

    def wait_until_healthy(self, run_id: str, *, timeout_s: float = 30.0) -> dict[str, object]:
        paths = DebugHarnessPaths.build(self.root, run_id)
        health_path = paths.observability_run_path / "current" / "health.json"
        deadline = time.time() + timeout_s
        last_state: dict[str, object] | None = None
        while time.time() < deadline:
            if health_path.exists():
                try:
                    last_state = json.loads(health_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    last_state = None
                if isinstance(last_state, dict) and last_state.get("status") == "healthy":
                    return last_state
            time.sleep(0.2)
        raise TimeoutError(f"Run {run_id} did not become healthy within {timeout_s:.1f}s")

    def is_port_listening(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _prepare_layout(self, paths: DebugHarnessPaths, *, purpose: str | None) -> None:
        for path in (
            paths.root,
            paths.manifests_dir,
            paths.cwd_dir,
            paths.data_dir,
            paths.run_dir,
            paths.cwd_path,
            paths.data_path,
            paths.observability_run_path,
        ):
            path.mkdir(parents=True, exist_ok=True)
        marker = {
            "purpose": purpose,
            "created_at": self.now(),
        }
        (paths.cwd_path / ".imcodex-debug-session.json").write_text(
            json.dumps(marker, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_manifest(self, path: Path, manifest: DebugRunManifest) -> None:
        path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def _read_manifest(self, run_id: str) -> DebugRunManifest:
        path = DebugHarnessPaths.build(self.root, run_id).manifest_path
        return DebugRunManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _generate_run_id(self) -> str:
        timestamp = self.now()
        timestamp = timestamp.split("+", 1)[0]
        timestamp = timestamp.replace("-", "").replace(":", "")
        if "." in timestamp:
            main, fractional = timestamp.split(".", 1)
            timestamp = f"{main.replace('T', '-')}-{fractional}"
        else:
            timestamp = timestamp.replace("T", "-")
        return f"debug-{timestamp}"

    def _next_run_id(self) -> str:
        base = self._generate_run_id()
        candidate = base
        index = 2
        manifests_dir = self.root / "manifests"
        while (manifests_dir / f"{candidate}.json").exists():
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _default_now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).astimezone().isoformat()

    def _default_launcher(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> object:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _terminate_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)

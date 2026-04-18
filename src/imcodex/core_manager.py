from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


Launcher = Callable[..., object]


@dataclass(slots=True)
class DedicatedCoreManifest:
    pid: int
    port: int
    url: str
    started_at: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DedicatedCoreManifest":
        return cls(
            pid=int(payload["pid"]),
            port=int(payload["port"]),
            url=str(payload["url"]),
            started_at=str(payload["started_at"]),
            status=str(payload["status"]),
        )


class DedicatedCoreManager:
    def __init__(
        self,
        *,
        root: Path,
        repo_root: Path,
        launcher: Launcher | None = None,
        now: Callable[[], str] | None = None,
        codex_bin: str = "codex",
    ) -> None:
        self.root = Path(root)
        self.repo_root = Path(repo_root)
        self.launcher = launcher or self._default_launcher
        self.now = now or self._default_now
        self.codex_bin = codex_bin
        self._process: object | None = None

    @property
    def manifest_path(self) -> Path:
        return self.root / "core.json"

    def start(self, *, port: int) -> DedicatedCoreManifest:
        self.root.mkdir(parents=True, exist_ok=True)
        url = f"ws://127.0.0.1:{port}"
        process = self.launcher(
            command=[self.codex_bin, "app-server", "--listen", url],
            cwd=self.repo_root,
            env=os.environ.copy(),
        )
        self._process = process
        manifest = DedicatedCoreManifest(
            pid=int(getattr(process, "pid")),
            port=port,
            url=url,
            started_at=self.now(),
            status="running",
        )
        self.manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return manifest

    def stop(self) -> DedicatedCoreManifest:
        manifest = self.status()
        process = self._process
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
        self.manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        self._process = None
        return manifest

    def status(self) -> DedicatedCoreManifest:
        return DedicatedCoreManifest.from_dict(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def wait_until_ready(self, *, port: int, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.2)
        raise TimeoutError(f"Dedicated core on port {port} did not become ready within {timeout_s:.1f}s")

    def _default_launcher(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> object:
        resolved = self._resolve_command(command)
        return subprocess.Popen(
            resolved,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _default_now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).astimezone().isoformat()

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

    def _resolve_command(self, command: list[str]) -> list[str]:
        if os.name != "nt" or not command:
            return command
        executable = command[0]
        if any(sep in executable for sep in ("\\", "/")):
            if executable.lower().endswith(".cmd"):
                return ["cmd.exe", "/c", executable, *command[1:]]
            return command
        if "." in executable:
            return command
        shim = shutil.which(f"{executable}.cmd")
        if shim:
            return ["cmd.exe", "/c", shim, *command[1:]]
        resolved = shutil.which(f"{executable}.exe")
        if resolved:
            return [resolved, *command[1:]]
        return command

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable


Launcher = Callable[..., object]
Stopper = Callable[[int], None]
HealthWaiter = Callable[[int, float], dict[str, object]]


class BridgeRestartExecutor:
    def __init__(
        self,
        *,
        launcher: Launcher | None = None,
        stopper: Stopper | None = None,
        health_waiter: HealthWaiter | None = None,
    ) -> None:
        self.launcher = launcher or self._default_launcher
        self.stopper = stopper or self._default_stopper
        self.health_waiter = health_waiter or self._default_health_waiter

    def restart(self, launch_snapshot_path: Path, *, timeout_s: float = 30.0) -> dict[str, object]:
        snapshot = json.loads(Path(launch_snapshot_path).read_text(encoding="utf-8"))
        pid = int(snapshot["pid"])
        port = int(snapshot["port"])
        command = [str(part) for part in snapshot["command"]]
        cwd = Path(str(snapshot["cwd"]))
        env = {str(key): str(value) for key, value in dict(snapshot["env"]).items()}

        self.stopper(pid)
        process = self.launcher(command=command, cwd=cwd, env=env)
        health = self.health_waiter(port, timeout_s)
        return {
            "pid": int(getattr(process, "pid", 0)),
            "port": port,
            "health": health,
        }

    def _default_launcher(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> object:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _default_stopper(self, pid: int) -> None:
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

    def _default_health_waiter(self, port: int, timeout_s: float) -> dict[str, object]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return {"status": "healthy", "port": port}
            time.sleep(0.2)
        raise TimeoutError(f"Bridge on port {port} did not become healthy within {timeout_s:.1f}s")

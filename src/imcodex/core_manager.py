from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


Launcher = Callable[..., object]
WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)
WINDOWS_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


@dataclass(slots=True)
class DedicatedCoreManifest:
    pid: int
    port: int
    url: str
    started_at: str
    status: str
    command: list[str] | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None

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
            command=list(payload["command"]) if isinstance(payload.get("command"), list) else None,
            stdout_log=str(payload["stdout_log"]) if payload.get("stdout_log") is not None else None,
            stderr_log=str(payload["stderr_log"]) if payload.get("stderr_log") is not None else None,
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
        command = [self.codex_bin, "app-server", "--listen", url]
        stdout_log = self.root / "core.stdout.log"
        stderr_log = self.root / "core.stderr.log"
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        process = self.launcher(
            command=command,
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
            command=command,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
        )
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        return manifest

    def stop(self) -> DedicatedCoreManifest:
        manifest = self.status()
        process = self._process
        if process is not None:
            self._terminate_running_process(process, manifest.pid)
            wait = getattr(process, "wait", None)
            if callable(wait):
                wait(timeout=10)
        else:
            self._terminate_pid(manifest.pid)
        manifest.status = "stopped"
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        self._process = None
        return manifest

    def status(self) -> DedicatedCoreManifest:
        return DedicatedCoreManifest.from_dict(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def verify(self, *, port: int) -> DedicatedCoreManifest:
        expected_url = f"ws://127.0.0.1:{port}"
        try:
            manifest = self.status()
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Port {port} is occupied, but no valid IMCodex core manifest owns it"
            ) from exc
        command_matches = bool(
            manifest.command
            and len(manifest.command) >= 4
            and manifest.command[-3:] == ["app-server", "--listen", expected_url]
        )
        if (
            manifest.status != "running"
            or manifest.port != port
            or manifest.url != expected_url
            or not command_matches
        ):
            raise RuntimeError(
                f"Port {port} is occupied, but the IMCodex core manifest does not match it"
            )
        if not self._process_exists(manifest.pid):
            raise RuntimeError(
                f"Port {port} is occupied, but the recorded IMCodex core PID {manifest.pid} is not running"
            )
        if not self._port_is_listening(port):
            raise RuntimeError(f"Recorded IMCodex core on port {port} is not listening")
        if not self._health_is_ready(port):
            raise RuntimeError(
                f"Recorded IMCodex core on port {port} did not pass its App Server readiness probe"
            )
        return manifest

    def wait_until_ready(self, *, port: int, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._port_is_listening(port):
                return
            time.sleep(0.2)
        raise TimeoutError(f"Dedicated core on port {port} did not become ready within {timeout_s:.1f}s")

    @staticmethod
    def _port_is_listening(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _process_exists(pid: int) -> bool:
        if os.name == "nt":
            return DedicatedCoreManager._windows_process_exists(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _health_is_ready(port: int) -> bool:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(
                f"http://127.0.0.1:{port}/readyz",
                timeout=1.0,
            ) as response:
                return 200 <= int(response.status) < 400
        except Exception:
            return False

    def _default_launcher(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> object:
        resolved = self._resolve_command(command)
        stdout_path = self.root / "core.stdout.log"
        stderr_path = self.root / "core.stderr.log"
        with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
            return subprocess.Popen(
                resolved,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                **self._detached_launcher_kwargs(),
            )

    @staticmethod
    def _detached_launcher_kwargs() -> dict[str, object]:
        if os.name == "nt":
            return {"creationflags": (WINDOWS_CREATE_NEW_PROCESS_GROUP | WINDOWS_DETACHED_PROCESS)}
        return {"start_new_session": True}

    def _default_now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).astimezone().isoformat()

    def _terminate_pid(self, pid: int) -> None:
        if os.name == "nt":
            self._terminate_windows_process_tree(pid)
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
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
        raise RuntimeError(f"Dedicated Codex core process group for PID {pid} did not stop")

    def _terminate_running_process(self, process: object, pid: int) -> None:
        if os.name == "nt":
            self._terminate_windows_process_tree(pid)
            return
        try:
            os.killpg(pid, signal.SIGTERM)
            return
        except OSError:
            pass
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()

    @staticmethod
    def _terminate_windows_process_tree(pid: int) -> None:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        try:
            process_exists = DedicatedCoreManager._windows_process_exists(pid)
        except OSError as exc:
            raise RuntimeError(f"Could not verify dedicated Codex core process tree for PID {pid}") from exc
        if not process_exists:
            return
        raise RuntimeError(f"Could not terminate dedicated Codex core process tree for PID {pid}")

    @staticmethod
    def _windows_process_exists(pid: int) -> bool:
        """Query a Windows PID without using os.kill(), which terminates there."""

        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            return True
        raise OSError(error, f"OpenProcess failed for PID {pid}")

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

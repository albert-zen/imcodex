from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


Launcher = Callable[..., object]
LiveProcessCommandVerifier = Callable[["DedicatedCoreManifest", set[int]], bool]
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
        live_process_command_verifier: LiveProcessCommandVerifier | None = None,
    ) -> None:
        self.root = Path(root)
        self.repo_root = Path(repo_root)
        self.launcher = launcher or self._default_launcher
        self.now = now or self._default_now
        self.codex_bin = codex_bin
        self.live_process_command_verifier = (
            live_process_command_verifier or self._live_process_command_matches
        )
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
        if manifest.status != "running":
            return manifest
        self._validate_manifest(manifest, port=manifest.port)
        process = self._process
        if process is not None:
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                return self._mark_stopped(manifest)
            self._terminate_running_process(process, manifest.pid)
            wait = getattr(process, "wait", None)
            if callable(wait):
                wait(timeout=10)
        else:
            if not self._process_exists(manifest.pid):
                return self._mark_stopped(manifest)
            self._verify_listener_identity(manifest)
            self._terminate_pid(manifest.pid)
        return self._mark_stopped(manifest)

    def status(self) -> DedicatedCoreManifest:
        return DedicatedCoreManifest.from_dict(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def verify(self, *, port: int) -> DedicatedCoreManifest:
        try:
            manifest = self.status()
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Port {port} is occupied, but no valid IMCodex core manifest owns it"
            ) from exc
        self._validate_manifest(manifest, port=port)
        listener_owner_pids = self._verify_listener_identity(manifest)
        try:
            command_matches = self.live_process_command_verifier(manifest, listener_owner_pids)
        except Exception as exc:
            raise RuntimeError(
                f"Could not verify the recorded IMCodex core process command on port {port}"
            ) from exc
        if not command_matches:
            raise RuntimeError(
                f"Recorded IMCodex core PID {manifest.pid} does not run the expected command"
            )
        if not self._health_is_ready(port):
            raise RuntimeError(
                f"Recorded IMCodex core on port {port} did not pass its App Server readiness probe"
            )
        return manifest

    def _validate_manifest(self, manifest: DedicatedCoreManifest, *, port: int) -> None:
        expected_url = f"ws://127.0.0.1:{port}"
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

    def _verify_listener_identity(self, manifest: DedicatedCoreManifest) -> set[int]:
        port = manifest.port
        if not self._process_exists(manifest.pid):
            raise RuntimeError(
                f"Port {port} is occupied, but the recorded IMCodex core PID {manifest.pid} is not running"
            )
        if not self._port_is_listening(port):
            raise RuntimeError(f"Recorded IMCodex core on port {port} is not listening")
        try:
            listener_owner_pids = self._listener_owner_pids_in_process_tree(manifest.pid, port)
        except Exception as exc:
            raise RuntimeError(
                f"Could not verify that recorded IMCodex core PID {manifest.pid} owns port {port}: {exc}"
            ) from exc
        if not listener_owner_pids:
            raise RuntimeError(
                f"Recorded IMCodex core PID {manifest.pid} does not own the listener on port {port}"
            )
        return listener_owner_pids

    def _mark_stopped(self, manifest: DedicatedCoreManifest) -> DedicatedCoreManifest:
        manifest.status = "stopped"
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._process = None
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
    def _listener_is_owned_by_process_tree(pid: int, port: int) -> bool:
        return bool(DedicatedCoreManager._listener_owner_pids_in_process_tree(pid, port))

    @staticmethod
    def _listener_owner_pids_in_process_tree(pid: int, port: int) -> set[int]:
        if os.name == "nt":
            owner_pids = DedicatedCoreManager._windows_tcp_listener_owner_pids(port)
            parent_by_pid = DedicatedCoreManager._windows_process_parent_map()
        elif sys.platform.startswith("linux"):
            owner_pids = DedicatedCoreManager._linux_tcp_listener_owner_pids(port)
            parent_by_pid = DedicatedCoreManager._linux_process_parent_map()
        elif sys.platform == "darwin":
            owner_pids = DedicatedCoreManager._macos_tcp_listener_owner_pids(port)
            parent_by_pid = DedicatedCoreManager._macos_process_parent_map()
        else:
            raise RuntimeError(
                f"listener ownership verification is unsupported on {sys.platform or os.name}"
            )
        return {
            owner_pid
            for owner_pid in owner_pids
            if DedicatedCoreManager._pid_is_same_or_descendant(owner_pid, pid, parent_by_pid)
        }

    @staticmethod
    def _pid_is_same_or_descendant(
        candidate_pid: int,
        root_pid: int,
        parent_by_pid: dict[int, int],
    ) -> bool:
        current = candidate_pid
        visited: set[int] = set()
        while current > 0 and current not in visited:
            if current == root_pid:
                return True
            visited.add(current)
            current = parent_by_pid.get(current, 0)
        return False

    @staticmethod
    def _linux_tcp_listener_owner_pids(port: int) -> set[int]:
        proc_root = Path("/proc")
        tcp_table = proc_root / "net" / "tcp"
        if not tcp_table.is_file():
            raise RuntimeError("Linux /proc/net/tcp is unavailable")
        try:
            rows = tcp_table.read_text(encoding="ascii").splitlines()[1:]
        except OSError as exc:
            raise RuntimeError(f"could not read Linux TCP listener table: {exc}") from exc

        listener_inodes: set[str] = set()
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                row_port = int(port_hex, 16)
            except (ValueError, IndexError):
                continue
            if address_hex == "0100007F" and row_port == port:
                listener_inodes.add(fields[9])
        if not listener_inodes:
            return set()

        owners: set[int] = set()
        try:
            process_dirs = list(proc_root.iterdir())
        except OSError as exc:
            raise RuntimeError(f"could not enumerate Linux processes: {exc}") from exc
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            try:
                descriptors = list((process_dir / "fd").iterdir())
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in listener_inodes:
                    owners.add(int(process_dir.name))
                    break
        return owners

    @staticmethod
    def _linux_process_parent_map() -> dict[int, int]:
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            raise RuntimeError("Linux /proc is unavailable")
        parents: dict[int, int] = {}
        try:
            process_dirs = list(proc_root.iterdir())
        except OSError as exc:
            raise RuntimeError(f"could not enumerate Linux processes: {exc}") from exc
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            try:
                stat = (process_dir / "stat").read_text(encoding="ascii")
                fields = stat[stat.rfind(")") + 1 :].split()
                parents[int(process_dir.name)] = int(fields[1])
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
                continue
            except OSError:
                continue
        return parents

    @staticmethod
    def _macos_tcp_listener_owner_pids(port: int) -> set[int]:
        lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else None)
        if lsof is None:
            raise RuntimeError("macOS listener ownership verification requires lsof")
        result = subprocess.run(
            [
                lsof,
                "-nP",
                f"-iTCP@127.0.0.1:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode not in {0, 1} or (result.returncode == 1 and result.stderr.strip()):
            detail = result.stderr.strip() or f"lsof exited with code {result.returncode}"
            raise RuntimeError(f"could not query macOS TCP listener ownership: {detail}")
        return {
            int(line[1:])
            for line in result.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()
        }

    @staticmethod
    def _macos_process_parent_map() -> dict[int, int]:
        ps = shutil.which("ps") or ("/bin/ps" if Path("/bin/ps").is_file() else None)
        if ps is None:
            raise RuntimeError("macOS listener ownership verification requires ps")
        result = subprocess.run(
            [ps, "-axo", "pid=,ppid="],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"ps exited with code {result.returncode}"
            raise RuntimeError(f"could not query macOS process ancestry: {detail}")
        parents: dict[int, int] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                child_pid, parent_pid = (int(value) for value in fields)
            except ValueError:
                continue
            parents[child_pid] = parent_pid
        return parents

    @staticmethod
    def _windows_tcp_listener_owner_pids(port: int) -> set[int]:
        import ctypes
        import struct
        from ctypes import wintypes

        class TcpRowOwnerPid(ctypes.Structure):
            _fields_ = [
                ("state", wintypes.DWORD),
                ("local_address", wintypes.DWORD),
                ("local_port", wintypes.DWORD),
                ("remote_address", wintypes.DWORD),
                ("remote_port", wintypes.DWORD),
                ("owning_pid", wintypes.DWORD),
            ]

        af_inet = 2
        table_owner_pid_listener = 3
        error_insufficient_buffer = 122
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        get_table = iphlpapi.GetExtendedTcpTable
        get_table.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.BOOL,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
        ]
        get_table.restype = wintypes.ULONG

        size = wintypes.ULONG(0)
        result = get_table(None, ctypes.byref(size), False, af_inet, table_owner_pid_listener, 0)
        if result not in {0, error_insufficient_buffer}:
            raise OSError(result, "GetExtendedTcpTable size query failed")
        buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(wintypes.DWORD)))
        result = get_table(buffer, ctypes.byref(size), False, af_inet, table_owner_pid_listener, 0)
        if result != 0:
            raise OSError(result, "GetExtendedTcpTable failed")

        count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        rows_type = TcpRowOwnerPid * count
        rows = rows_type.from_address(ctypes.addressof(buffer) + ctypes.sizeof(wintypes.DWORD))
        owners: set[int] = set()
        for row in rows:
            local_address = socket.inet_ntoa(struct.pack("<L", row.local_address))
            local_port = socket.ntohs(row.local_port & 0xFFFF)
            if local_address == "127.0.0.1" and local_port == port:
                owners.add(int(row.owning_pid))
        return owners

    @staticmethod
    def _windows_process_parent_map() -> dict[int, int]:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("process_id", wintypes.DWORD),
                ("default_heap_id", ctypes.c_size_t),
                ("module_id", wintypes.DWORD),
                ("threads", wintypes.DWORD),
                ("parent_process_id", wintypes.DWORD),
                ("priority_class_base", wintypes.LONG),
                ("flags", wintypes.DWORD),
                ("executable", wintypes.WCHAR * 260),
            ]

        snapshot_process = 0x00000002
        invalid_handle = ctypes.c_void_p(-1).value
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_process, 0)
        if snapshot == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateToolhelp32Snapshot failed")
        parents: dict[int, int] = {}
        try:
            entry = ProcessEntry32W()
            entry.size = ctypes.sizeof(ProcessEntry32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                raise OSError(error, "Process32FirstW failed")
            while True:
                parents[int(entry.process_id)] = int(entry.parent_process_id)
                entry.size = ctypes.sizeof(ProcessEntry32W)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error != 18:  # ERROR_NO_MORE_FILES
                        raise OSError(error, "Process32NextW failed")
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return parents

    def _live_process_command_matches(
        self,
        manifest: DedicatedCoreManifest,
        listener_owner_pids: set[int],
    ) -> bool:
        if os.name != "nt":
            return True
        records = self._windows_process_records()
        expected_name = Path(str((manifest.command or [self.codex_bin])[0])).stem.casefold()
        expected_url = manifest.url.casefold()
        for record in records:
            pid = int(record["process_id"])
            if pid not in listener_owner_pids:
                continue
            arguments = self._windows_command_line_arguments(str(record.get("command_line") or ""))
            normalized_arguments = [argument.casefold() for argument in arguments]
            expected_tail = ["app-server", "--listen", expected_url]
            has_expected_tail = any(
                normalized_arguments[index : index + 3] == expected_tail
                for index in range(max(0, len(normalized_arguments) - 2))
            )
            identity_values = [
                *arguments,
                str(record.get("executable_path") or ""),
                str(record.get("name") or ""),
            ]
            identity_stems = {
                Path(value.strip('"')).stem.casefold()
                for value in identity_values
                if value.strip('"')
            }
            if expected_name in identity_stems and has_expected_tail:
                return True
        return False

    @staticmethod
    def _windows_command_line_arguments(command_line: str) -> list[str]:
        if not command_line.strip():
            return []
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        argument_count = ctypes.c_int()
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        arguments = shell32.CommandLineToArgvW(command_line, ctypes.byref(argument_count))
        if not arguments:
            error = ctypes.get_last_error()
            raise OSError(error, "CommandLineToArgvW failed")
        try:
            return [arguments[index] for index in range(argument_count.value)]
        finally:
            kernel32.LocalFree(arguments)

    @staticmethod
    def _windows_process_records() -> list[dict[str, object]]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")
        if powershell is None:
            raise RuntimeError("PowerShell is unavailable for process command verification")
        script = (
            "$ErrorActionPreference='Stop'; "
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,"
            "ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            raise RuntimeError("Windows process inventory query failed")
        payload = json.loads(completed.stdout or "[]")
        rows = payload if isinstance(payload, list) else [payload]
        records: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                process_id = int(row.get("ProcessId"))
                parent_process_id = int(row.get("ParentProcessId"))
            except (TypeError, ValueError):
                continue
            records.append(
                {
                    "process_id": process_id,
                    "parent_process_id": parent_process_id,
                    "name": row.get("Name"),
                    "executable_path": row.get("ExecutablePath"),
                    "command_line": row.get("CommandLine"),
                }
            )
        return records

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

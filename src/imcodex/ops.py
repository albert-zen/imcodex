from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .config import (
    DOTENV_IMPORTED_KEYS_ENV,
    KNOWN_SETTING_ENV_KEYS,
    LAUNCHER_RELOADABLE_KEYS_ENV,
    PREFLIGHT_CURRENT_HTTP_HOST_ENV,
    PREFLIGHT_CURRENT_HTTP_PORT_ENV,
    TARGET_ENVIRONMENT_KEYS,
    _read_dotenv,
    is_restart_context_env_key,
)
from .observability.health import (
    BRIDGE_HEALTH_KIND,
    BRIDGE_INSTANCE_HEADER,
    BRIDGE_SHUTDOWN_PATH,
)


Launcher = Callable[..., object]
Stopper = Callable[[int], None]
Preflight = Callable[..., None]
CurrentVerifier = Callable[[str, int, int, str, float], None]
HealthWaiter = Callable[[str, int, object, float], dict[str, object]]
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREFLIGHT_CODE = (
    "from imcodex.composition import run_runtime_preflight; "
    "raise SystemExit(run_runtime_preflight())"
)
_PROVENANCE_KEYS = {
    DOTENV_IMPORTED_KEYS_ENV,
    LAUNCHER_RELOADABLE_KEYS_ENV,
    PREFLIGHT_CURRENT_HTTP_HOST_ENV,
    PREFLIGHT_CURRENT_HTTP_PORT_ENV,
}


class BridgeRestartExecutor:
    def __init__(
        self,
        *,
        launcher: Launcher | None = None,
        stopper: Stopper | None = None,
        preflight: Preflight | None = None,
        current_verifier: CurrentVerifier | None = None,
        health_waiter: HealthWaiter | None = None,
    ) -> None:
        self.launcher = launcher or self._default_launcher
        self.stopper = stopper
        self.preflight = preflight or self._default_preflight
        self.current_verifier = current_verifier or self._default_current_verifier
        self.health_waiter = health_waiter or self._default_health_waiter

    def restart(self, launch_snapshot_path: Path, *, timeout_s: float = 30.0) -> dict[str, object]:
        snapshot = json.loads(Path(launch_snapshot_path).read_text(encoding="utf-8"))
        if snapshot.get("restartSupported", True) is not True:
            raise RuntimeError(
                "Bridge restart is unavailable because this runtime was built from explicit Settings."
            )
        pid = int(snapshot["pid"])
        current_port = int(snapshot["port"])
        current_host = self._snapshot_http_host(snapshot)
        current_instance_id = str(snapshot.get("instanceId") or "")
        command = [str(part) for part in snapshot["command"]]
        if not command:
            raise ValueError("Launch snapshot command must not be empty")
        cwd = Path(str(snapshot["cwd"]))
        if "requiredExternalEnvKeys" in snapshot:
            env = self._reconstructed_environment(snapshot, cwd=cwd)
            port = self._http_port(env)
        else:
            env, port = self._legacy_environment(snapshot, cwd=cwd)
        host = self._http_host(env)

        # Re-run all local startup validation in the exact reconstructed
        # context before taking down the still-healthy bridge.
        self.preflight(
            command=command,
            cwd=cwd,
            env=env,
            current_host=current_host,
            current_port=current_port,
        )
        self.current_verifier(current_host, current_port, pid, current_instance_id, 2.0)
        if self.stopper is not None:
            self.stopper(pid)
        else:
            self._default_stopper(
                pid,
                host=current_host,
                port=current_port,
                instance_id=current_instance_id,
            )
        process = self.launcher(command=command, cwd=cwd, env=env)
        health = self.health_waiter(host, port, process, timeout_s)
        return {
            "pid": int(getattr(process, "pid", 0)),
            "host": host,
            "port": port,
            "health": health,
        }

    def _reconstructed_environment(
        self,
        snapshot: dict,
        *,
        cwd: Path,
    ) -> dict[str, str]:
        required_external = self._required_external_environment_keys(snapshot)
        caller_env = os.environ.copy()
        self._validate_external_environment(required_external, environ=caller_env)

        dotenv_imported = self._snapshot_key_set(snapshot, "dotenvImportedKeys")
        launcher_reloadable = self._snapshot_key_set(snapshot, "launcherReloadableKeys")
        previously_reloadable = dotenv_imported | launcher_reloadable
        env = caller_env.copy()
        for key in _PROVENANCE_KEYS:
            env.pop(key, None)
        for key in list(env):
            if (
                key in KNOWN_SETTING_ENV_KEYS or is_restart_context_env_key(key)
            ) and key not in required_external:
                env.pop(key, None)
        for key in previously_reloadable:
            if key not in required_external:
                env.pop(key, None)

        dotenv = _read_dotenv(cwd / ".env")
        target_is_external = bool(required_external & TARGET_ENVIRONMENT_KEYS)
        current_imported: set[str] = set()
        for key, value in dotenv.items():
            if key in _PROVENANCE_KEYS or _ENVIRONMENT_NAME.fullmatch(key) is None:
                continue
            if "\x00" in value:
                raise ValueError(f"Dotenv value for {key} contains a NUL character")
            if target_is_external and key in TARGET_ENVIRONMENT_KEYS:
                continue
            if key in required_external:
                continue
            if (
                key in KNOWN_SETTING_ENV_KEYS
                or is_restart_context_env_key(key)
                or key in previously_reloadable
                or key not in env
            ):
                env[key] = value
                current_imported.add(key)

        env[DOTENV_IMPORTED_KEYS_ENV] = ",".join(sorted(current_imported))
        return env

    def _legacy_environment(
        self,
        snapshot: dict,
        *,
        cwd: Path,
    ) -> tuple[dict[str, str], int]:
        port = int(snapshot["port"])
        snapshot_env = {str(key): str(value) for key, value in dict(snapshot["env"]).items()}
        reload_env_keys = {str(key) for key in snapshot.get("reloadEnvKeys", []) if str(key).strip()}
        env = os.environ.copy()
        for key in _PROVENANCE_KEYS:
            env.pop(key, None)
        for key in reload_env_keys:
            env.pop(key, None)
        env.update({key: value for key, value in snapshot_env.items() if key not in reload_env_keys})
        dotenv = _read_dotenv(cwd / ".env")
        for key in reload_env_keys:
            value = dotenv.get(key)
            if value is None:
                continue
            if _ENVIRONMENT_NAME.fullmatch(key) is None or "\x00" in value:
                raise ValueError(f"Dotenv value for {key} is not a valid process setting")
            env[key] = value
        default_port = 8000 if "IMCODEX_HTTP_PORT" in reload_env_keys else port
        port = int(env.get("IMCODEX_HTTP_PORT", default_port))
        return env, port

    def _required_external_environment_keys(self, snapshot: dict) -> set[str]:
        keys = self._snapshot_key_set(snapshot, "requiredExternalEnvKeys")
        unknown = {
            key
            for key in keys
            if key not in KNOWN_SETTING_ENV_KEYS and not is_restart_context_env_key(key)
        }
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise ValueError(f"Launch snapshot contains unknown external settings: {rendered}")
        return keys

    def _validate_external_environment(
        self,
        required_keys: set[str],
        *,
        environ: dict[str, str],
    ) -> None:
        missing = sorted(key for key in required_keys if key not in environ)
        if not missing:
            return
        raise RuntimeError("Restart environment is missing required external settings: " + ", ".join(missing))

    def _snapshot_key_set(self, snapshot: dict, field: str) -> set[str]:
        raw = snapshot.get(field, [])
        if not isinstance(raw, list):
            raise ValueError(f"Launch snapshot {field} must be a list")
        keys: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or _ENVIRONMENT_NAME.fullmatch(item) is None:
                raise ValueError(f"Launch snapshot {field} contains an invalid key")
            if item in _PROVENANCE_KEYS:
                raise ValueError(f"Launch snapshot {field} contains an internal key")
            keys.add(item)
        return keys

    @staticmethod
    def _http_port(environ: dict[str, str]) -> int:
        try:
            port = int(environ.get("IMCODEX_HTTP_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("IMCODEX_HTTP_PORT must be an integer between 1 and 65535") from exc
        if not 1 <= port <= 65535:
            raise ValueError("IMCODEX_HTTP_PORT must be an integer between 1 and 65535")
        return port

    @staticmethod
    def _http_host(environ: dict[str, str]) -> str:
        host = environ.get("IMCODEX_HTTP_HOST", "0.0.0.0").strip()
        if not host:
            raise ValueError("IMCODEX_HTTP_HOST must not be empty")
        return host

    @classmethod
    def _snapshot_http_host(cls, snapshot: dict) -> str:
        configured = str(snapshot.get("host") or "").strip()
        if not configured:
            configured = str(dict(snapshot.get("env") or {}).get("IMCODEX_HTTP_HOST") or "").strip()
        return cls._http_host({"IMCODEX_HTTP_HOST": configured or "0.0.0.0"})

    def _default_current_verifier(
        self,
        host: str,
        port: int,
        expected_pid: int,
        expected_instance_id: str,
        timeout_s: float,
    ) -> None:
        if not expected_instance_id:
            raise RuntimeError(
                "Launch snapshot lacks a bridge instance identity; restart manually once to refresh it."
            )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                payload = self._read_health_payload(host, port, timeout_s=0.5)
            except (OSError, UnicodeError, ValueError):
                time.sleep(0.1)
                continue
            if self._health_identity_matches(
                payload,
                expected_pid=expected_pid,
                expected_instance_id=expected_instance_id,
            ):
                return
            raise RuntimeError(
                "Launch snapshot does not identify the bridge currently listening on its recorded endpoint."
            )
        raise RuntimeError(
            "The bridge recorded by the launch snapshot could not be verified before restart."
        )

    def _default_preflight(
        self,
        *,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        current_host: str | None = None,
        current_port: int | None = None,
    ) -> None:
        preflight_env = env.copy()
        if current_host is not None:
            preflight_env[PREFLIGHT_CURRENT_HTTP_HOST_ENV] = current_host
        if current_port is not None:
            preflight_env[PREFLIGHT_CURRENT_HTTP_PORT_ENV] = str(current_port)
        try:
            completed = subprocess.run(
                [command[0], "-c", _PREFLIGHT_CODE],
                cwd=str(cwd),
                env=preflight_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Bridge restart preflight could not complete") from exc
        if completed.returncode != 0:
            detail = self._safe_preflight_diagnostic(completed.stderr)
            suffix = f": {detail}" if detail else f" with exit code {completed.returncode}"
            raise RuntimeError(f"Bridge restart preflight failed{suffix}")

    @staticmethod
    def _safe_preflight_diagnostic(stderr: str | None) -> str:
        lines = [" ".join(line.split()) for line in str(stderr or "").splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[-1][:500]

    def _default_launcher(self, *, command: list[str], cwd: Path, env: dict[str, str]) -> object:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def _default_stopper(
        self,
        pid: int,
        *,
        host: str,
        port: int,
        instance_id: str,
    ) -> None:
        if os.name == "nt":
            self._request_windows_graceful_shutdown(
                pid,
                host=host,
                port=port,
                instance_id=instance_id,
            )
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise RuntimeError("Could not request graceful bridge shutdown.") from exc
        deadline = time.time() + 10
        while time.time() < deadline:
            if not self._posix_process_is_running(pid):
                return
            time.sleep(0.1)
        raise RuntimeError("The running bridge did not finish graceful shutdown within 10 seconds.")

    def _request_windows_graceful_shutdown(
        self,
        pid: int,
        *,
        host: str,
        port: int,
        instance_id: str,
    ) -> None:
        probe_host = self._probe_host(host)
        if not self._is_loopback_host(probe_host):
            raise RuntimeError(
                "Native Windows restart requires the bridge HTTP listener to be reachable through loopback."
            )
        if not instance_id:
            raise RuntimeError("Native Windows restart requires a current bridge instance identity.")
        authority = f"[{probe_host}]" if ":" in probe_host else probe_host
        request = urllib.request.Request(
            f"http://{authority}:{port}{BRIDGE_SHUTDOWN_PATH}",
            data=b"",
            headers={BRIDGE_INSTANCE_HEADER: instance_id},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=2.0) as response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                response.read(4096)
        except OSError as exc:
            raise RuntimeError(
                "The running bridge did not accept a graceful Windows shutdown request."
            ) from exc
        if status != 202:
            raise RuntimeError(
                "The running bridge did not accept a graceful Windows shutdown request."
            )
        deadline = time.time() + 10
        while time.time() < deadline:
            if not self._windows_process_is_running(pid):
                return
            time.sleep(0.1)
        raise RuntimeError(
            "The running bridge did not finish graceful Windows shutdown within 10 seconds."
        )

    @staticmethod
    def _posix_process_is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False

        # A zombie still has a PID, so kill(pid, 0) reports success even
        # though it has already released its listener and cannot do any more
        # shutdown work.  When the restart executor is also the process's
        # parent (as in the live debug harness), observe that state without
        # reaping it: the owner of the Popen handle must remain responsible
        # for the eventual wait().
        waitid = getattr(os, "waitid", None)
        wait_nowait = getattr(os, "WNOWAIT", None)
        if callable(waitid) and wait_nowait is not None:
            wait_flags = os.WEXITED | os.WNOHANG | wait_nowait
            try:
                exited = waitid(os.P_PID, pid, wait_flags)
            except (ChildProcessError, PermissionError):
                # Most production restart executors are not the bridge's
                # parent.  In that case the process supervisor will reap it
                # and the normal kill(pid, 0) check remains authoritative.
                pass
            except OSError:
                # WNOWAIT is not implemented by every POSIX runtime.  Fall
                # back conservatively rather than reaping with waitpid().
                pass
            else:
                if exited is not None:
                    return False
                return True

        # If this executor is not the process's parent, waitid cannot inspect
        # it.  POSIX ps exposes zombie state without changing parentage or
        # consuming the exit status.  Use fixed system paths rather than the
        # restart caller's PATH.
        ps = next(
            (
                candidate
                for candidate in (Path("/bin/ps"), Path("/usr/bin/ps"))
                if candidate.is_file()
            ),
            None,
        )
        if ps is None:
            return True
        try:
            completed = subprocess.run(
                [str(ps), "-o", "stat=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        state = completed.stdout.strip().split(maxsplit=1)
        if state:
            return not state[0].upper().startswith("Z")

        # The process may have been reaped between kill(pid, 0) and ps.  A
        # second signal check distinguishes that race from an unusable ps.
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _windows_process_is_running(pid: int) -> bool:
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            if ctypes.get_last_error() == error_invalid_parameter:
                return False
            raise RuntimeError("Could not verify graceful Windows bridge shutdown.")
        exit_code = wintypes.DWORD()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
            return address.is_loopback or bool(
                getattr(address, "ipv4_mapped", None)
                and address.ipv4_mapped.is_loopback
            )
        except ValueError:
            return host.rstrip(".").lower() == "localhost"

    def _default_health_waiter(
        self,
        host: str,
        port: int,
        process: object,
        timeout_s: float,
    ) -> dict[str, object]:
        expected_pid = int(getattr(process, "pid", 0))
        if expected_pid <= 0:
            raise RuntimeError("Replacement bridge process did not expose a valid PID")
        probe_host = self._probe_host(host)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            returncode = self._process_returncode(process)
            if returncode is not None:
                raise RuntimeError(
                    f"Replacement bridge process {expected_pid} exited before becoming healthy"
                )
            try:
                payload = self._read_health_payload(host, port, timeout_s=0.5)
                instance_id = payload.get("instanceId")
                if self._health_identity_matches(payload, expected_pid=expected_pid):
                    return {
                        "status": "healthy",
                        "host": probe_host,
                        "port": port,
                        "pid": expected_pid,
                        "instanceId": instance_id,
                    }
            except (OSError, UnicodeError, ValueError):
                pass
            time.sleep(0.2)
        returncode = self._process_returncode(process)
        if returncode is not None:
            raise RuntimeError(
                f"Replacement bridge process {expected_pid} exited before becoming healthy"
            )
        raise TimeoutError(
            f"Replacement bridge on {probe_host}:{port} did not become healthy within {timeout_s:.1f}s"
        )

    def _read_health_payload(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float,
    ) -> dict[str, object]:
        probe_host = self._probe_host(host)
        authority = f"[{probe_host}]" if ":" in probe_host else probe_host
        request = urllib.request.Request(
            f"http://{authority}:{port}/healthz",
            method="GET",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout_s) as response:
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("Bridge health response is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bridge health response is not an object")
        return payload

    @staticmethod
    def _health_identity_matches(
        payload: dict[str, object],
        *,
        expected_pid: int,
        expected_instance_id: str | None = None,
    ) -> bool:
        response_pid = payload.get("pid")
        instance_id = payload.get("instanceId")
        return (
            payload.get("kind") == BRIDGE_HEALTH_KIND
            and payload.get("status") == "healthy"
            and isinstance(response_pid, int)
            and not isinstance(response_pid, bool)
            and response_pid == expected_pid
            and isinstance(instance_id, str)
            and bool(instance_id)
            and (expected_instance_id is None or instance_id == expected_instance_id)
        )

    @staticmethod
    def _process_returncode(process: object) -> int | None:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return None
        return poll()

    @staticmethod
    def _probe_host(bind_host: str) -> str:
        normalized = bind_host.strip().removeprefix("[").removesuffix("]")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return normalized
        if not address.is_unspecified:
            return normalized
        return "::1" if address.version == 6 else "127.0.0.1"

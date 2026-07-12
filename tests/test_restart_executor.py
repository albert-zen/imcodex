from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imcodex.ops import BridgeRestartExecutor


@pytest.mark.skipif(os.name != "posix", reason="POSIX process states only")
def test_posix_process_status_treats_unreaped_child_as_stopped_without_reaping() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while BridgeRestartExecutor._posix_process_is_running(process.pid):
            if time.monotonic() >= deadline:
                pytest.fail("child did not reach its exited, unreaped state")
            time.sleep(0.01)

        # WNOWAIT must leave the Popen owner able to perform the eventual
        # reap; kill(pid, 0) still succeeds while that zombie is present.
        os.kill(process.pid, 0)
        assert process.returncode is None
    finally:
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process states only")
@pytest.mark.parametrize(("state", "expected"), [("Z+\n", False), ("Ss\n", True)])
def test_posix_process_status_reads_non_child_zombie_state(
    monkeypatch,
    state: str,
    expected: bool,
) -> None:
    def non_child(*_args):
        raise ChildProcessError

    monkeypatch.setattr("imcodex.ops.os.kill", lambda *_args: None)
    monkeypatch.setattr("imcodex.ops.os.waitid", non_child)
    monkeypatch.setattr(
        "imcodex.ops.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=state),
    )

    assert BridgeRestartExecutor._posix_process_is_running(12345) is expected


def test_restart_executor_reads_launch_snapshot_and_restarts_bridge(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    def starter(*, command: list[str], cwd: Path, env: dict[str, str]):
        calls.append(("start", {"command": command, "cwd": cwd, "env": env}))

        class _Process:
            pid = 65432

        return _Process()

    def stopper(pid: int) -> None:
        calls.append(("stop", pid))

    def preflight(
        *,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        current_host: str,
        current_port: int,
    ) -> None:
        calls.append(
            (
                "preflight",
                {
                    "command": command,
                    "cwd": cwd,
                    "env": env,
                    "current_host": current_host,
                    "current_port": current_port,
                },
            )
        )

    def verify(host: str, port: int, pid: int, instance_id: str, timeout_s: float) -> None:
        calls.append(
            (
                "verify",
                {
                    "host": host,
                    "port": port,
                    "pid": pid,
                    "instance_id": instance_id,
                    "timeout_s": timeout_s,
                },
            )
        )

    def waiter(host: str, port: int, process: object, timeout_s: float) -> dict[str, object]:
        calls.append(
            (
                "wait",
                {
                    "host": host,
                    "port": port,
                    "pid": getattr(process, "pid", None),
                    "timeout_s": timeout_s,
                },
            )
        )
        return {"status": "healthy", "port": port}

    launch_snapshot = {
        "command": ["python", "-m", "imcodex"],
        "cwd": r"D:\desktop\imcodex",
        "env": {
            "IMCODEX_HTTP_PORT": "8000",
            "IMCODEX_CORE_MODE": "dedicated-ws",
            "IMCODEX_CORE_URL": "ws://127.0.0.1:8765",
        },
        "pid": 44584,
        "port": 8000,
    }
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(json.dumps(launch_snapshot), encoding="utf-8")

    executor = BridgeRestartExecutor(
        launcher=starter,
        stopper=stopper,
        preflight=preflight,
        current_verifier=verify,
        health_waiter=waiter,
    )

    result = executor.restart(launch_path, timeout_s=15.0)

    assert result["health"]["status"] == "healthy"
    assert calls[0][0] == "preflight"
    assert calls[0][1]["command"] == ["python", "-m", "imcodex"]
    assert calls[1] == (
        "verify",
        {
            "host": "0.0.0.0",
            "port": 8000,
            "pid": 44584,
            "instance_id": "",
            "timeout_s": 2.0,
        },
    )
    assert calls[2] == ("stop", 44584)
    assert calls[3][0] == "start"
    assert calls[3][1]["command"] == ["python", "-m", "imcodex"]
    assert calls[4] == (
        "wait",
        {"host": "0.0.0.0", "port": 8000, "pid": 65432, "timeout_s": 15.0},
    )


def test_restart_executor_reloads_dotenv_owned_values_and_new_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("IMCODEX_HTTP_PORT", "8000")
    monkeypatch.setenv("IMCODEX_QQ_ENABLED", "0")
    monkeypatch.setenv("IMCODEX_APP_SERVER_URL", "unix://")
    monkeypatch.setenv("HTTPS_PROXY", "http://old-proxy.example")
    monkeypatch.setenv("IMCODEX_QQ_ALLOWED_USER_IDS", "caller-only")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "IMCODEX_HTTP_PORT=8123\n"
        "IMCODEX_HTTP_HOST=192.0.2.20\n"
        "IMCODEX_QQ_ENABLED=1\n"
        "IMCODEX_APP_SERVER_URL=stdio://\n"
        "HTTPS_PROXY=http://new-proxy.example\n"
        "SSL_CERT_FILE=/current/company.pem\n",
        encoding="utf-8",
    )
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {
                    "IMCODEX_HTTP_PORT": "8000",
                    "IMCODEX_QQ_ENABLED": "0",
                    "IMCODEX_LOG_LEVEL": "DEBUG",
                },
                "reloadEnvKeys": [
                    "HTTPS_PROXY",
                    "IMCODEX_APP_SERVER_URL",
                    "IMCODEX_HTTP_PORT",
                    "IMCODEX_QQ_ENABLED",
                ],
                "dotenvImportedKeys": [
                    "HTTPS_PROXY",
                    "IMCODEX_HTTP_PORT",
                    "IMCODEX_QQ_ENABLED",
                ],
                "launcherReloadableKeys": ["IMCODEX_APP_SERVER_URL"],
                "requiredExternalEnvKeys": [],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )

    def starter(*, command: list[str], cwd: Path, env: dict[str, str]):
        calls.append(("start", {"command": command, "cwd": cwd, "env": env}))
        return type("Process", (), {"pid": 65432})()

    executor = BridgeRestartExecutor(
        launcher=starter,
        stopper=lambda pid: calls.append(("stop", pid)),
        preflight=lambda **_kwargs: None,
        current_verifier=lambda *_args: None,
        health_waiter=lambda host, port, process, timeout: {
            "status": "healthy",
            "host": host,
            "port": port,
            "pid": process.pid,
            "timeout": timeout,
        },
    )

    result = executor.restart(launch_path, timeout_s=15.0)

    started_env = calls[1][1]["env"]
    assert started_env["IMCODEX_HTTP_PORT"] == "8123"
    assert started_env["IMCODEX_HTTP_HOST"] == "192.0.2.20"
    assert started_env["IMCODEX_QQ_ENABLED"] == "1"
    assert started_env["IMCODEX_APP_SERVER_URL"] == "stdio://"
    assert started_env["HTTPS_PROXY"] == "http://new-proxy.example"
    assert started_env["SSL_CERT_FILE"] == "/current/company.pem"
    assert "IMCODEX_LOG_LEVEL" not in started_env
    assert "IMCODEX_QQ_ALLOWED_USER_IDS" not in started_env
    assert "IMCODEX_LAUNCHER_RELOADABLE_KEYS" not in started_env
    assert set(started_env["IMCODEX_DOTENV_IMPORTED_KEYS"].split(",")) == {
        "HTTPS_PROXY",
        "IMCODEX_APP_SERVER_URL",
        "IMCODEX_HTTP_HOST",
        "IMCODEX_HTTP_PORT",
        "IMCODEX_QQ_ENABLED",
        "SSL_CERT_FILE",
    }
    assert result["port"] == 8123
    assert result["host"] == "192.0.2.20"
    assert result["health"]["port"] == 8123


def test_restart_executor_aborts_when_reconstructed_configuration_preflight_fails(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": [],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )

    def reject_preflight(**_kwargs) -> None:
        calls.append("preflight")
        raise RuntimeError("invalid channel configuration")

    executor = BridgeRestartExecutor(
        launcher=lambda **_kwargs: calls.append("start"),
        stopper=lambda _pid: calls.append("stop"),
        preflight=reject_preflight,
        health_waiter=lambda *_args: {},
    )

    with pytest.raises(RuntimeError, match="invalid channel configuration"):
        executor.restart(launch_path)

    assert calls == ["preflight"]


def test_restart_executor_rejects_explicit_settings_snapshot_before_stop(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "restartSupported": False,
                "settingsSource": "explicit",
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )
    executor = BridgeRestartExecutor(
        launcher=lambda **_kwargs: calls.append("start"),
        stopper=lambda _pid: calls.append("stop"),
        preflight=lambda **_kwargs: calls.append("preflight"),
        health_waiter=lambda *_args: {},
    )

    with pytest.raises(RuntimeError, match="explicit Settings"):
        executor.restart(launch_path)

    assert calls == []


def test_restart_executor_aborts_when_current_bridge_identity_is_stale(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "host": "127.0.0.1",
                "instanceId": "stale-instance",
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": [],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )

    def reject_stale(*_args) -> None:
        calls.append("verify")
        raise RuntimeError("snapshot is stale")

    executor = BridgeRestartExecutor(
        launcher=lambda **_kwargs: calls.append("start"),
        stopper=lambda _pid: calls.append("stop"),
        preflight=lambda **_kwargs: calls.append("preflight"),
        current_verifier=reject_stale,
        health_waiter=lambda *_args: {},
    )

    with pytest.raises(RuntimeError, match="snapshot is stale"):
        executor.restart(launch_path)

    assert calls == ["preflight", "verify"]


def test_default_restart_preflight_uses_reconstructed_process_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("imcodex.ops.subprocess.run", run)
    env = {"IMCODEX_HTTP_PORT": "8123", "EXTERNAL_MARKER": "kept"}

    BridgeRestartExecutor()._default_preflight(
        command=["/runtime/python", "-m", "imcodex"],
        cwd=tmp_path,
        env=env,
    )

    assert observed["command"][0:2] == ["/runtime/python", "-c"]
    assert "run_runtime_preflight" in observed["command"][2]
    assert observed["cwd"] == str(tmp_path)
    assert observed["env"] == env
    assert observed["env"] is not env
    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.PIPE
    assert observed["text"] is True
    assert observed["check"] is False


def test_default_restart_preflight_preserves_safe_failure_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "imcodex.ops.subprocess.run",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stderr": "RuntimeError: Codex executable was not found\n",
            },
        )(),
    )

    with pytest.raises(RuntimeError, match="Codex executable was not found"):
        BridgeRestartExecutor()._default_preflight(
            command=["/runtime/python", "-m", "imcodex"],
            cwd=tmp_path,
            env={},
        )


def test_restart_executor_uses_default_when_dotenv_port_was_removed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IMCODEX_HTTP_PORT", "8123")
    (tmp_path / ".env").write_text("IMCODEX_HTTP_HOST=192.0.2.30\n", encoding="utf-8")
    started: list[dict[str, str]] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {"IMCODEX_HTTP_PORT": "8123"},
                "reloadEnvKeys": ["IMCODEX_HTTP_HOST", "IMCODEX_HTTP_PORT"],
                "dotenvImportedKeys": ["IMCODEX_HTTP_PORT"],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": [],
                "pid": 44584,
                "port": 8123,
            }
        ),
        encoding="utf-8",
    )

    executor = BridgeRestartExecutor(
        launcher=lambda **kwargs: started.append(kwargs["env"]) or type("Process", (), {"pid": 65432})(),
        stopper=lambda pid: None,
        preflight=lambda **_kwargs: None,
        current_verifier=lambda *_args: None,
        health_waiter=lambda host, port, process, timeout: {"host": host, "port": port, "timeout": timeout},
    )

    result = executor.restart(launch_path)

    assert result["port"] == 8000
    assert result["host"] == "192.0.2.30"
    assert "IMCODEX_HTTP_PORT" not in started[0]
    assert started[0]["IMCODEX_HTTP_HOST"] == "192.0.2.30"


@pytest.mark.parametrize(
    "key",
    ["IMCODEX_INBOUND_WEBHOOK_TOKEN", "CODEX_HOME", "SSL_CERT_FILE"],
)
def test_restart_executor_rejects_missing_external_settings_before_stop(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    monkeypatch.delenv(key, raising=False)
    calls: list[tuple[str, object]] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "reloadEnvKeys": [],
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": [key],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )
    executor = BridgeRestartExecutor(
        launcher=lambda **kwargs: calls.append(("start", kwargs)),
        stopper=lambda pid: calls.append(("stop", pid)),
        preflight=lambda **_kwargs: calls.append(("preflight", _kwargs)),
        health_waiter=lambda host, port, process, timeout: {"host": host, "port": port},
    )

    with pytest.raises(RuntimeError, match=key):
        executor.restart(launch_path)

    assert calls == []


def test_restart_executor_preserves_valid_external_secret_without_snapshot_plaintext(
    tmp_path: Path,
    monkeypatch,
) -> None:
    key = "IMCODEX_APP_SERVER_AUTH_TOKEN"
    secret = "external-token-value"
    monkeypatch.setenv(key, secret)
    monkeypatch.setenv("CODEX_HOME", "/private/native-codex-home")
    (tmp_path / ".env").write_text(f"{key}=dotenv-token\n", encoding="utf-8")
    started: list[dict[str, str]] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "reloadEnvKeys": [],
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": ["CODEX_HOME", key],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )
    executor = BridgeRestartExecutor(
        launcher=lambda **kwargs: started.append(kwargs["env"]) or type("Process", (), {"pid": 65432})(),
        stopper=lambda pid: None,
        preflight=lambda **_kwargs: None,
        current_verifier=lambda *_args: None,
        health_waiter=lambda host, port, process, timeout: {"host": host, "port": port},
    )

    executor.restart(launch_path)

    assert secret not in launch_path.read_text(encoding="utf-8")
    assert started[0][key] == secret
    assert started[0]["CODEX_HOME"] == "/private/native-codex-home"


def test_restart_executor_preserves_external_target_group_over_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IMCODEX_CORE_MODE", "spawned-stdio")
    monkeypatch.setenv("IMCODEX_APP_SERVER_URL", "caller-noise")
    (tmp_path / ".env").write_text(
        "IMCODEX_APP_SERVER_URL=unix://\nIMCODEX_HTTP_PORT=8124\n",
        encoding="utf-8",
    )
    started: list[dict[str, str]] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "reloadEnvKeys": [],
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": ["IMCODEX_CORE_MODE"],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )
    executor = BridgeRestartExecutor(
        launcher=lambda **kwargs: started.append(kwargs["env"]) or type("Process", (), {"pid": 65432})(),
        stopper=lambda pid: None,
        preflight=lambda **_kwargs: None,
        current_verifier=lambda *_args: None,
        health_waiter=lambda host, port, process, timeout: {"host": host, "port": port},
    )

    result = executor.restart(launch_path)

    assert started[0]["IMCODEX_CORE_MODE"] == "spawned-stdio"
    assert "IMCODEX_APP_SERVER_URL" not in started[0]
    assert result["port"] == 8124


def test_restart_executor_rejects_invalid_dotenv_port_before_stop(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "IMCODEX_HTTP_PORT=not-a-port\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, object]] = []
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "command": ["python", "-m", "imcodex"],
                "cwd": str(tmp_path),
                "env": {},
                "reloadEnvKeys": [],
                "dotenvImportedKeys": [],
                "launcherReloadableKeys": [],
                "requiredExternalEnvKeys": [],
                "pid": 44584,
                "port": 8000,
            }
        ),
        encoding="utf-8",
    )
    executor = BridgeRestartExecutor(
        launcher=lambda **kwargs: calls.append(("start", kwargs)),
        stopper=lambda pid: calls.append(("stop", pid)),
        preflight=lambda **_kwargs: calls.append(("preflight", _kwargs)),
        health_waiter=lambda host, port, process, timeout: {"host": host, "port": port},
    )

    with pytest.raises(ValueError, match="integer between 1 and 65535"):
        executor.restart(launch_path)

    assert calls == []


def test_default_health_waiter_requires_bridge_identity_and_replacement_pid(
    monkeypatch,
) -> None:
    requests: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {
                    "kind": "imcodex.bridge",
                    "status": "healthy",
                    "pid": 65432,
                    "instanceId": "instance-65432",
                }
            ).encode()

    class _Opener:
        def open(self, request, *, timeout: float):
            assert timeout == 0.5
            requests.append(request.full_url)
            return _Response()

    class _Process:
        pid = 65432

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("imcodex.ops.urllib.request.build_opener", lambda *_args: _Opener())

    health = BridgeRestartExecutor()._default_health_waiter("::", 8123, _Process(), 1.0)

    assert requests == ["http://[::1]:8123/healthz"]
    assert health == {
        "status": "healthy",
        "host": "::1",
        "port": 8123,
        "pid": 65432,
        "instanceId": "instance-65432",
    }


def test_default_current_verifier_requires_exact_pid_and_instance(monkeypatch) -> None:
    executor = BridgeRestartExecutor()
    payload = {
        "kind": "imcodex.bridge",
        "status": "healthy",
        "pid": 44584,
        "instanceId": "instance-44584",
    }
    monkeypatch.setattr(executor, "_read_health_payload", lambda *_args, **_kwargs: payload)

    executor._default_current_verifier(
        "127.0.0.1",
        8000,
        44584,
        "instance-44584",
        1.0,
    )

    with pytest.raises(RuntimeError, match="does not identify"):
        executor._default_current_verifier(
            "127.0.0.1",
            8000,
            44584,
            "different-instance",
            1.0,
        )


def test_windows_stopper_requests_loopback_graceful_shutdown(monkeypatch) -> None:
    requests: list[tuple[str, str | None]] = []

    class _Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"status":"shutting_down"}'

    class _Opener:
        @staticmethod
        def open(request, *, timeout: float):
            assert timeout == 2.0
            requests.append(
                (
                    request.full_url,
                    request.get_header("X-imcodex-instance"),
                )
            )
            return _Response()

    executor = BridgeRestartExecutor()
    running = iter([True, False])
    monkeypatch.setattr("imcodex.ops.urllib.request.build_opener", lambda *_args: _Opener())
    monkeypatch.setattr(executor, "_windows_process_is_running", lambda _pid: next(running))
    monkeypatch.setattr("imcodex.ops.time.sleep", lambda _seconds: None)

    executor._request_windows_graceful_shutdown(
        44584,
        host="0.0.0.0",
        port=8000,
        instance_id="instance-44584",
    )

    assert requests == [
        (
            "http://127.0.0.1:8000/_imcodex/ops/shutdown",
            "instance-44584",
        )
    ]


def test_windows_stopper_fails_closed_without_loopback() -> None:
    with pytest.raises(RuntimeError, match="reachable through loopback"):
        BridgeRestartExecutor()._request_windows_graceful_shutdown(
            44584,
            host="192.0.2.20",
            port=8000,
            instance_id="instance-44584",
        )


def test_default_windows_stopper_never_force_terminates(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    executor = BridgeRestartExecutor()
    monkeypatch.setattr("imcodex.ops.os.name", "nt")
    monkeypatch.setattr(
        executor,
        "_request_windows_graceful_shutdown",
        lambda pid, **kwargs: calls.append((pid, kwargs)),
    )
    monkeypatch.setattr(
        "imcodex.ops.os.kill",
        lambda *_args: pytest.fail("Windows restart must not call os.kill"),
    )

    executor._default_stopper(
        44584,
        host="0.0.0.0",
        port=8000,
        instance_id="instance-44584",
    )

    assert calls == [
        (
            44584,
            {
                "host": "0.0.0.0",
                "port": 8000,
                "instance_id": "instance-44584",
            },
        )
    ]


def test_default_health_waiter_does_not_accept_an_unrelated_listener(
    monkeypatch,
) -> None:
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit: int) -> bytes:
            return json.dumps(
                {
                    "kind": "imcodex.bridge",
                    "status": "healthy",
                    "pid": 11111,
                    "instanceId": "unrelated",
                }
            ).encode()

    class _Opener:
        @staticmethod
        def open(_request, *, timeout: float):
            assert timeout == 0.5
            return _Response()

    class _Process:
        pid = 65432

        @staticmethod
        def poll():
            return None

    moments = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr("imcodex.ops.urllib.request.build_opener", lambda *_args: _Opener())
    monkeypatch.setattr("imcodex.ops.time.time", lambda: next(moments))
    monkeypatch.setattr("imcodex.ops.time.sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="did not become healthy"):
        BridgeRestartExecutor()._default_health_waiter("127.0.0.1", 8123, _Process(), 0.5)


def test_default_health_waiter_fails_fast_when_replacement_exits(monkeypatch) -> None:
    class _Process:
        pid = 65432

        @staticmethod
        def poll():
            return 1

    class _Opener:
        @staticmethod
        def open(*_args, **_kwargs):
            raise AssertionError("health request should not run")

    monkeypatch.setattr("imcodex.ops.urllib.request.build_opener", lambda *_args: _Opener())

    with pytest.raises(RuntimeError, match="exited before becoming healthy"):
        BridgeRestartExecutor()._default_health_waiter("127.0.0.1", 8123, _Process(), 1.0)

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import imcodex.core_manager as core_manager_module
from imcodex.core_manager import DedicatedCoreManager


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited = True
        return 0


def test_core_manager_start_writes_manifest_and_uses_ws_listener(tmp_path: Path) -> None:
    launched: dict[str, object] = {}

    def launcher(*, command: list[str], cwd: Path, env: dict[str, str]):
        launched["command"] = command
        launched["cwd"] = cwd
        launched["env"] = env
        return _FakeProcess(pid=42001)

    manager = DedicatedCoreManager(
        root=tmp_path / "core-lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=launcher,
        now=lambda: "2026-04-19T12:00:01+08:00",
    )

    manifest = manager.start(port=8765)

    assert manifest.pid == 42001
    assert manifest.port == 8765
    assert manifest.url == "ws://127.0.0.1:8765"
    assert manifest.command == ["codex", "app-server", "--listen", "ws://127.0.0.1:8765"]
    assert manifest.stdout_log is not None
    assert manifest.stderr_log is not None
    assert launched["command"] == ["codex", "app-server", "--listen", "ws://127.0.0.1:8765"]
    saved = json.loads((tmp_path / "core-lab" / "core.json").read_text(encoding="utf-8"))
    assert saved["url"] == "ws://127.0.0.1:8765"
    assert saved["status"] == "running"
    assert saved["stdout_log"].endswith("core.stdout.log")
    assert saved["stderr_log"].endswith("core.stderr.log")
    assert (tmp_path / "core-lab" / "core.stdout.log").exists()
    assert (tmp_path / "core-lab" / "core.stderr.log").exists()


def test_core_manager_stop_updates_manifest(tmp_path: Path) -> None:
    process = _FakeProcess(pid=42001)

    def launcher(*, command: list[str], cwd: Path, env: dict[str, str]):
        del command, cwd, env
        return process

    manager = DedicatedCoreManager(
        root=tmp_path / "core-lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=launcher,
        now=lambda: "2026-04-19T12:00:01+08:00",
    )

    manifest = manager.start(port=8765)
    stopped = manager.stop()

    assert stopped.pid == manifest.pid
    assert stopped.status == "stopped"
    assert process.terminated is True
    assert process.waited is True


def test_core_manager_resolves_windows_codex_cmd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("imcodex.core_manager.os.name", "nt")
    monkeypatch.setattr(
        "imcodex.core_manager.shutil.which",
        lambda name: r"C:\Users\xmly\AppData\Roaming\npm\codex.cmd" if name == "codex.cmd" else None,
    )

    manager = DedicatedCoreManager(root=tmp_path / "core-lab", repo_root=Path(r"D:\desktop\imcodex"))

    resolved = manager._resolve_command(["codex", "app-server", "--listen", "ws://127.0.0.1:8765"])

    assert resolved == [
        "cmd.exe",
        "/c",
        r"C:\Users\xmly\AppData\Roaming\npm\codex.cmd",
        "app-server",
        "--listen",
        "ws://127.0.0.1:8765",
    ]


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_core_manager_default_launcher_detaches_from_parent_process_group(
    monkeypatch,
    tmp_path: Path,
    platform_name: str,
) -> None:
    observed: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return _FakeProcess(pid=42002)

    manager = DedicatedCoreManager(
        root=tmp_path / "core-lab",
        repo_root=tmp_path,
    )
    manager.root.mkdir(parents=True)
    monkeypatch.setattr(core_manager_module.os, "name", platform_name)
    monkeypatch.setattr(core_manager_module.subprocess, "Popen", fake_popen)

    manager._default_launcher(
        command=["codex.exe", "app-server", "--listen", "ws://127.0.0.1:8765"],
        cwd=tmp_path,
        env={"PATH": "test"},
    )

    assert observed["stdin"] is core_manager_module.subprocess.DEVNULL
    if platform_name == "nt":
        creationflags = int(observed["creationflags"])
        assert creationflags & core_manager_module.WINDOWS_CREATE_NEW_PROCESS_GROUP
        assert creationflags & core_manager_module.WINDOWS_DETACHED_PROCESS
        assert "start_new_session" not in observed
    else:
        assert observed["start_new_session"] is True
        assert "creationflags" not in observed


def test_core_manager_terminates_complete_windows_process_tree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return Completed()

    manager = DedicatedCoreManager(
        root=tmp_path / "core-lab",
        repo_root=tmp_path,
    )
    monkeypatch.setattr(core_manager_module.os, "name", "nt")
    monkeypatch.setattr(core_manager_module.subprocess, "run", fake_run)

    manager._terminate_pid(42001)

    assert observed["command"] == [
        "taskkill",
        "/PID",
        "42001",
        "/T",
        "/F",
    ]
    assert observed["check"] is False
    assert observed["stdin"] is core_manager_module.subprocess.DEVNULL
    assert observed["stdout"] is core_manager_module.subprocess.DEVNULL
    assert observed["stderr"] is core_manager_module.subprocess.DEVNULL


def test_core_manager_windows_taskkill_failure_uses_nondestructive_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class Failed:
        returncode = 1

    manager = DedicatedCoreManager(root=tmp_path / "core-lab", repo_root=tmp_path)
    monkeypatch.setattr(core_manager_module.subprocess, "run", lambda *_args, **_kwargs: Failed())
    monkeypatch.setattr(
        DedicatedCoreManager,
        "_windows_process_exists",
        staticmethod(lambda _pid: False),
    )

    manager._terminate_windows_process_tree(42001)


@pytest.mark.skipif(os.name != "nt", reason="Windows process API")
def test_core_manager_windows_process_probe_handles_64_bit_handle(tmp_path: Path) -> None:
    manager = DedicatedCoreManager(root=tmp_path / "core-lab", repo_root=tmp_path)

    assert manager._windows_process_exists(os.getpid()) is True


def test_core_manager_reports_posix_process_that_ignores_sigterm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manager = DedicatedCoreManager(root=tmp_path / "core-lab", repo_root=tmp_path)
    now = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(core_manager_module.os, "name", "posix")
    monkeypatch.setattr(core_manager_module.os, "killpg", lambda _pid, _signal: None)
    monkeypatch.setattr(core_manager_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(core_manager_module.time, "time", lambda: next(now))
    monkeypatch.setattr(core_manager_module.time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="did not stop"):
        manager._terminate_pid(42001)

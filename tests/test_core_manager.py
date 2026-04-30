from __future__ import annotations

import json
from pathlib import Path

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
    monkeypatch.setattr("imcodex.core_manager.shutil.which", lambda name: r"C:\Users\xmly\AppData\Roaming\npm\codex.cmd" if name == "codex.cmd" else None)

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

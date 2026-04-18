from __future__ import annotations

import json
import subprocess
from pathlib import Path

import imcodex.debug_harness.manager as manager_module
from imcodex.debug_harness.manager import DebugInstanceManager


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


def test_start_creates_isolated_layout_and_manifest(tmp_path: Path) -> None:
    launched: dict[str, object] = {}

    def launcher(*, command: list[str], cwd: Path, env: dict[str, str]):
        launched["command"] = command
        launched["cwd"] = cwd
        launched["env"] = env
        return _FakeProcess(pid=51234)

    manager = DebugInstanceManager(
        root=tmp_path / "lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=launcher,
        now=lambda: "2026-04-19T10:30:01+08:00",
    )

    manifest = manager.start(port=8011, purpose="restart-gap")

    assert manifest.run_id.startswith("debug-20260419-103001")
    assert manifest.port == 8011
    assert Path(manifest.cwd).is_dir()
    assert Path(manifest.data_dir).is_dir()
    assert Path(manifest.run_dir).is_dir()
    assert (Path(manifest.cwd) / ".imcodex-debug-session.json").is_file()
    assert launched["command"] == ["python", "-m", "imcodex"]
    assert launched["cwd"] == Path(r"D:\desktop\imcodex")
    env = launched["env"]
    assert env["IMCODEX_HTTP_PORT"] == "8011"
    assert env["IMCODEX_QQ_ENABLED"] == "0"
    assert env["IMCODEX_DEBUG_API_ENABLED"] == "1"
    assert env["IMCODEX_DATA_DIR"] == manifest.data_dir
    assert env["IMCODEX_RUN_DIR"] == manifest.run_dir

    saved = json.loads((tmp_path / "lab" / "manifests" / f"{manifest.run_id}.json").read_text(encoding="utf-8"))
    assert saved["purpose"] == "restart-gap"
    assert saved["status"] == "running"


def test_stop_terminates_known_run_and_updates_manifest(tmp_path: Path) -> None:
    process = _FakeProcess(pid=51234)
    launched: dict[str, _FakeProcess] = {}

    def launcher(*, command: list[str], cwd: Path, env: dict[str, str]):
        del command, cwd, env
        launched["process"] = process
        return process

    manager = DebugInstanceManager(
        root=tmp_path / "lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=launcher,
        now=lambda: "2026-04-19T10:30:01+08:00",
    )

    manifest = manager.start(port=8011)
    stopped = manager.stop(manifest.run_id)

    assert stopped.status == "stopped"
    assert process.terminated is True
    assert process.waited is True

    saved = json.loads((tmp_path / "lab" / "manifests" / f"{manifest.run_id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "stopped"


def test_list_runs_reads_existing_manifests(tmp_path: Path) -> None:
    manager = DebugInstanceManager(
        root=tmp_path / "lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=lambda **_: _FakeProcess(pid=1),
        now=lambda: "2026-04-19T10:30:01+08:00",
    )
    manifests_dir = tmp_path / "lab" / "manifests"
    manifests_dir.mkdir(parents=True)
    for run_id, port in (("debug-2", 8012), ("debug-1", 8011)):
        (manifests_dir / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "pid": 1,
                    "port": port,
                    "purpose": None,
                    "cwd": "cwd",
                    "data_dir": "data",
                    "run_dir": "run",
                    "started_at": "2026-04-19T10:30:01+08:00",
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )

    runs = manager.list_runs()

    assert [run.run_id for run in runs] == ["debug-1", "debug-2"]


def test_start_generates_unique_run_ids_when_timestamp_collides(tmp_path: Path) -> None:
    def launcher(*, command: list[str], cwd: Path, env: dict[str, str]):
        del command, cwd, env
        return _FakeProcess(pid=51234)

    manager = DebugInstanceManager(
        root=tmp_path / "lab",
        repo_root=Path(r"D:\desktop\imcodex"),
        launcher=launcher,
        now=lambda: "2026-04-19T10:30:01+08:00",
    )

    first = manager.start(port=8011)
    second = manager.start(port=8012)

    assert first.run_id == "debug-20260419-103001"
    assert second.run_id == "debug-20260419-103001-2"


def test_default_launcher_detaches_child_stdio(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command, *, cwd, env, stdout, stderr):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return _FakeProcess(pid=12345)

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    manager = DebugInstanceManager(root=Path(r"D:\tmp\lab"), repo_root=Path(r"D:\desktop\imcodex"))

    process = manager._default_launcher(
        command=["python", "-m", "imcodex"],
        cwd=Path(r"D:\desktop\imcodex"),
        env={"A": "B"},
    )

    assert process.pid == 12345
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL

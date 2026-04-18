from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from imcodex.observability.runtime import ObservabilityRuntime


def _clock() -> datetime:
    return datetime(2026, 4, 19, 10, 15, 30, tzinfo=timezone.utc)


def test_observability_runtime_creates_instance_archive_and_current_metadata(tmp_path: Path) -> None:
    runtime = ObservabilityRuntime(
        run_root=tmp_path,
        service_name="imcodex",
        log_level="INFO",
        http_host="0.0.0.0",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=_clock,
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {"git_branch": "main", "git_commit": "abc1234"},
    )

    runtime.start()

    assert runtime.instance_id == "20260419-101530-p48648"
    assert runtime.paths.instance_dir.is_dir()
    assert runtime.paths.current_dir.is_dir()

    archived = json.loads(runtime.paths.instance_metadata_path.read_text(encoding="utf-8"))
    current = json.loads(runtime.paths.current_metadata_path.read_text(encoding="utf-8"))

    assert archived["instance_id"] == runtime.instance_id
    assert current["instance_id"] == runtime.instance_id
    assert archived["git_branch"] == "main"
    assert archived["git_commit"] == "abc1234"
    assert archived["http_port"] == 8000
    runtime.stop()


def test_observability_runtime_writes_structured_events_to_archive_and_current(tmp_path: Path) -> None:
    runtime = ObservabilityRuntime(
        run_root=tmp_path,
        service_name="imcodex",
        log_level="INFO",
        http_host="0.0.0.0",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=_clock,
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {"git_branch": "main", "git_commit": "abc1234"},
    )

    runtime.start()
    runtime.emit_event(
        component="bridge",
        event="bridge.started",
        message="Bridge startup complete",
        data={"status": "ok"},
    )

    archived_lines = runtime.paths.events_path.read_text(encoding="utf-8").strip().splitlines()
    current_lines = runtime.paths.current_events_path.read_text(encoding="utf-8").strip().splitlines()

    archived = json.loads(archived_lines[-1])
    current = json.loads(current_lines[-1])

    assert archived["event"] == "bridge.started"
    assert current["event"] == "bridge.started"
    assert archived["instance_id"] == runtime.instance_id
    assert archived["data"]["status"] == "ok"
    runtime.stop()


def test_observability_runtime_prunes_old_archived_runs(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    for name in (
        "20260419-090000-p10000",
        "20260419-091000-p10001",
        "20260419-092000-p10002",
    ):
        (runs_dir / name).mkdir()

    runtime = ObservabilityRuntime(
        run_root=tmp_path,
        service_name="imcodex",
        log_level="INFO",
        http_host="0.0.0.0",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=_clock,
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {"git_branch": "main", "git_commit": "abc1234"},
        retention=2,
    )

    runtime.start()

    remaining = sorted(path.name for path in runs_dir.iterdir() if path.is_dir())
    assert remaining == [
        "20260419-092000-p10002",
        "20260419-101530-p48648",
    ]
    runtime.stop()

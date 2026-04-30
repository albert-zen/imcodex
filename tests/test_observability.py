from __future__ import annotations

import json
import time
from threading import Event
from threading import Thread
from datetime import datetime, timezone
from pathlib import Path

from imcodex.observability.context import InstanceContext
from imcodex.observability.events import EventWriter
from imcodex.observability.paths import ObservabilityPaths
from imcodex.observability.runtime import ObservabilityRuntime


def _clock() -> datetime:
    return datetime(2026, 4, 19, 10, 15, 30, tzinfo=timezone.utc)


def _context() -> InstanceContext:
    return InstanceContext(
        instance_id="instance-1",
        pid=48648,
        started_at=_clock().isoformat(),
        service_name="imcodex",
        cwd=r"D:\desktop\imcodex",
        git_branch="main",
        git_commit="abc1234",
        python_version="3.13",
        http_host="127.0.0.1",
        http_port=8000,
        app_server_url=None,
    )


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
    runtime.event_writer.flush()

    archived_lines = runtime.paths.events_path.read_text(encoding="utf-8").strip().splitlines()
    current_lines = runtime.paths.current_events_path.read_text(encoding="utf-8").strip().splitlines()

    archived = json.loads(archived_lines[-1])
    current = json.loads(current_lines[-1])

    assert archived["event"] == "bridge.started"
    assert current["event"] == "bridge.started"
    assert archived["instance_id"] == runtime.instance_id
    assert archived["data"]["status"] == "ok"
    runtime.stop()


def test_observability_runtime_writes_raw_protocol_only_when_enabled(tmp_path: Path) -> None:
    disabled = ObservabilityRuntime(
        run_root=tmp_path / "disabled",
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

    disabled.start()
    disabled.write_raw_protocol_message(
        stage="received",
        connection_mode="spawned-stdio",
        connection_epoch=1,
        payload={"method": "error", "params": {"error": {"message": "secret raw value"}}},
    )
    assert not disabled.paths.raw_protocol_path.exists()
    assert not disabled.paths.current_raw_protocol_path.exists()
    disabled.stop()

    enabled = ObservabilityRuntime(
        run_root=tmp_path / "enabled",
        service_name="imcodex",
        log_level="INFO",
        http_host="0.0.0.0",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=_clock,
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {"git_branch": "main", "git_commit": "abc1234"},
        raw_protocol_log_enabled=True,
    )

    enabled.start()
    enabled.write_raw_protocol_message(
        stage="received",
        connection_mode="spawned-stdio",
        connection_epoch=1,
        payload={"method": "error", "params": {"error": {"message": "secret raw value"}}},
    )
    assert enabled.raw_protocol_writer is not None
    enabled.raw_protocol_writer.flush()

    archived = json.loads(enabled.paths.raw_protocol_path.read_text(encoding="utf-8").strip())
    current = json.loads(enabled.paths.current_raw_protocol_path.read_text(encoding="utf-8").strip())

    assert archived == current
    assert archived["stage"] == "received"
    assert archived["connection_mode"] == "spawned-stdio"
    assert archived["connection_epoch"] == 1
    assert archived["payload"]["params"]["error"]["message"] == "secret raw value"
    enabled.stop()


def test_event_writer_flushes_events_from_background_writer(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    writer_started = Event()
    release_writer = Event()

    class SlowEventWriter(EventWriter):
        def _write_payload(self, payload: str) -> None:
            writer_started.set()
            release_writer.wait(timeout=1)
            super()._write_payload(payload)

    writer = SlowEventWriter(paths=paths, context=_context(), clock=_clock)

    started_at = time.perf_counter()
    writer.emit(component="bridge", event="bridge.started")
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    assert writer_started.wait(timeout=1)
    assert paths.events_path.read_text(encoding="utf-8") == ""

    release_writer.set()
    writer.flush()

    archived = json.loads(paths.events_path.read_text(encoding="utf-8").strip())
    assert archived["event"] == "bridge.started"
    writer.close()


def test_event_writer_close_does_not_leave_event_behind_sentinel(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    writer = EventWriter(paths=paths, context=_context(), clock=_clock)
    original_put = writer._queue.put
    record_put_started = Event()
    release_record_put = Event()

    def gated_put(item, *args, **kwargs):
        if isinstance(item, dict) and item.get("event") == "bridge.race":
            record_put_started.set()
            release_record_put.wait(timeout=1)
        return original_put(item, *args, **kwargs)

    writer._queue.put = gated_put  # type: ignore[method-assign]

    emit_thread = Thread(
        target=writer.emit,
        kwargs={"component": "bridge", "event": "bridge.race"},
    )
    emit_thread.start()
    assert record_put_started.wait(timeout=1)

    close_thread = Thread(target=writer.close)
    close_thread.start()
    release_record_put.set()

    emit_thread.join(timeout=1)
    close_thread.join(timeout=1)

    assert not emit_thread.is_alive()
    assert not close_thread.is_alive()
    archived = json.loads(paths.events_path.read_text(encoding="utf-8").strip())
    assert archived["event"] == "bridge.race"


def test_event_writer_continues_after_write_failure(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    write_failed = Event()

    class FlakyEventWriter(EventWriter):
        def __init__(self, **kwargs) -> None:
            self.attempts = 0
            super().__init__(**kwargs)

        def _write_payload(self, payload: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                write_failed.set()
                raise OSError("transient write failure")
            super()._write_payload(payload)

    writer = FlakyEventWriter(paths=paths, context=_context(), clock=_clock)

    writer.emit(component="bridge", event="bridge.first")
    assert write_failed.wait(timeout=1)
    writer._thread.join(timeout=0.05)
    assert writer._thread.is_alive()

    writer.emit(component="bridge", event="bridge.second")
    writer.flush()

    archived = json.loads(paths.events_path.read_text(encoding="utf-8").strip())
    assert archived["event"] == "bridge.second"
    writer.close()


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

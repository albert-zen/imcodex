from __future__ import annotations

import json
import logging
import time
from threading import Event
from threading import Thread
from datetime import datetime, timezone
from pathlib import Path

from imcodex.observability.context import InstanceContext
from imcodex.observability.events import EventWriter
from imcodex.observability.health import HealthWriter
from imcodex.observability.logger import _AsyncFanoutHandler
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
    original_put = writer._queue.put_nowait
    record_put_started = Event()
    release_record_put = Event()

    def gated_put(item, *args, **kwargs):
        if isinstance(item, dict) and item.get("event") == "bridge.race":
            record_put_started.set()
            release_record_put.wait(timeout=1)
        return original_put(item, *args, **kwargs)

    writer._queue.put_nowait = gated_put  # type: ignore[method-assign]

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


def test_health_writer_coalesces_slow_disk_writes_off_caller_thread(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    writer = HealthWriter(paths=paths, context=_context(), clock=_clock)
    writer_started = Event()
    release_writer = Event()
    original_write = writer._write_payload

    def slow_write(state) -> None:
        writer_started.set()
        release_writer.wait(timeout=1)
        original_write(state)

    writer._write_payload = slow_write  # type: ignore[method-assign]
    started_at = time.perf_counter()
    writer.merge_channel("qq", connected=True)
    writer.merge_channel("qq", status="connected")
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    assert writer_started.wait(timeout=1)
    release_writer.set()
    writer.flush()

    health = json.loads(paths.health_path.read_text(encoding="utf-8"))
    assert health["channels"]["qq"] == {"connected": True, "status": "connected"}
    writer.close()


def test_async_log_handler_keeps_slow_targets_off_caller_thread() -> None:
    target_started = Event()
    release_target = Event()

    class SlowHandler(logging.Handler):
        def emit(self, _record: logging.LogRecord) -> None:
            target_started.set()
            release_target.wait(timeout=1)

    handler = _AsyncFanoutHandler([SlowHandler()], level=logging.INFO)
    record = logging.LogRecord("imcodex.test", logging.INFO, __file__, 1, "hello", (), None)

    started_at = time.perf_counter()
    handler.emit(record)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    assert target_started.wait(timeout=1)
    release_target.set()
    handler.flush()
    handler.close()


def test_event_writer_drops_overflow_instead_of_growing_without_bound(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    writer_started = Event()
    release_writer = Event()

    class TinySlowWriter(EventWriter):
        QUEUE_LIMIT = 1

        def _write_payload(self, payload: str) -> None:
            writer_started.set()
            release_writer.wait(timeout=1)
            super()._write_payload(payload)

    writer = TinySlowWriter(paths=paths, context=_context(), clock=_clock)
    writer.emit(component="bridge", event="first")
    assert writer_started.wait(timeout=1)
    writer.emit(component="bridge", event="second")
    writer.emit(component="bridge", event="third")

    assert writer._dropped == 1
    release_writer.set()
    writer.close()


def test_observability_writers_bound_close_when_disk_is_stuck(tmp_path: Path) -> None:
    paths = ObservabilityPaths.build(tmp_path, "instance-1")
    paths.instance_dir.mkdir(parents=True)
    paths.current_dir.mkdir(parents=True)
    event_started = Event()
    health_started = Event()
    release_writers = Event()

    class StuckEventWriter(EventWriter):
        def _write_payload(self, payload: str) -> None:
            event_started.set()
            release_writers.wait(timeout=2)
            super()._write_payload(payload)

    event_writer = StuckEventWriter(paths=paths, context=_context(), clock=_clock)
    event_writer.emit(component="bridge", event="stuck")
    assert event_started.wait(timeout=1)

    health_writer = HealthWriter(paths=paths, context=_context(), clock=_clock)
    original_health_write = health_writer._write_payload

    def stuck_health_write(state) -> None:
        health_started.set()
        release_writers.wait(timeout=2)
        original_health_write(state)

    health_writer._write_payload = stuck_health_write  # type: ignore[method-assign]
    health_writer.update(status="stuck")
    assert health_started.wait(timeout=1)

    started_at = time.perf_counter()
    event_writer.close()
    health_writer.close()
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.8
    release_writers.set()
    event_writer._thread.join(timeout=1)
    health_writer._thread.join(timeout=1)
    assert json.loads(paths.health_path.read_text(encoding="utf-8"))["status"] == "stuck"


def test_async_log_handler_bounds_close_when_target_is_stuck() -> None:
    target_started = Event()
    release_target = Event()

    class StuckHandler(logging.Handler):
        def emit(self, _record: logging.LogRecord) -> None:
            target_started.set()
            release_target.wait(timeout=2)

    handler = _AsyncFanoutHandler([StuckHandler()], level=logging.INFO)
    record = logging.LogRecord("imcodex.test", logging.INFO, __file__, 1, "hello", (), None)
    handler.emit(record)
    assert target_started.wait(timeout=1)

    started_at = time.perf_counter()
    handler.close()
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.5
    assert handler._thread.is_alive()
    release_target.set()
    handler._thread.join(timeout=1)


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

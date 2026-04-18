from __future__ import annotations

import json
from pathlib import Path

import pytest

from imcodex.config import Settings
from imcodex.composition import build_runtime
from imcodex.observability.runtime import ObservabilityRuntime
from imcodex.runtime import AppRuntime


class _FakeClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def add_notification_handler(self, _handler) -> None:
        self.calls.append("client.add_notification_handler")

    def add_server_request_handler(self, _handler) -> None:
        self.calls.append("client.add_server_request_handler")

    async def connect(self) -> None:
        self.calls.append("client.connect")

    async def close(self) -> None:
        self.calls.append("client.close")


class _FakeService:
    def handle_notification(self, *_args, **_kwargs) -> None:
        return None

    async def handle_server_request(self, *_args, **_kwargs) -> None:
        return None


class _FakeChannel:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def start(self) -> None:
        self.calls.append("channel.start")

    async def stop(self) -> None:
        self.calls.append("channel.stop")


class _FakeObservability:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> None:
        self.calls.append("obs.start")

    def emit_event(self, *, component: str, event: str, **_kwargs) -> None:
        self.calls.append(f"obs.event:{component}:{event}")

    def update_health(self, **_kwargs) -> None:
        self.calls.append("obs.health")

    def stop(self) -> None:
        self.calls.append("obs.stop")


class _FailingClient(_FakeClient):
    async def connect(self) -> None:
        self.calls.append("client.connect")
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_app_runtime_wraps_startup_and_shutdown_with_observability_events() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[_FakeChannel(calls)],
        observability=_FakeObservability(calls),
    )

    await runtime.start()
    await runtime.stop()

    assert calls == [
        "obs.start",
        "obs.event:bridge:bridge.starting",
        "client.add_notification_handler",
        "client.add_server_request_handler",
        "client.connect",
        "channel.start",
        "obs.health",
        "obs.event:bridge:bridge.started",
        "obs.event:bridge:bridge.stopping",
        "channel.stop",
        "client.close",
        "obs.health",
        "obs.event:bridge:bridge.stopped",
        "obs.stop",
    ]


def test_build_runtime_constructs_observability_runtime(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".imcodex",
        run_dir=tmp_path / ".imcodex-run",
        codex_bin="codex",
        app_server_url=None,
        debug_api_enabled=False,
        log_level="INFO",
        http_host="127.0.0.1",
        http_port=8000,
        outbound_url=None,
        service_name="imcodex",
        qq_enabled=False,
        qq_app_id="",
        qq_client_secret="",
        qq_api_base="https://api.sgroup.qq.com",
    )

    runtime = build_runtime(settings)

    assert runtime.observability is not None
    assert runtime.observability.run_root == settings.run_dir
    assert runtime.observability.service_name == settings.service_name


@pytest.mark.asyncio
async def test_app_runtime_persists_lifecycle_events_and_health_snapshot(tmp_path: Path) -> None:
    observability = ObservabilityRuntime(
        run_root=tmp_path,
        service_name="imcodex",
        log_level="INFO",
        http_host="127.0.0.1",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=lambda: __import__("datetime").datetime(2026, 4, 19, 10, 15, 30, tzinfo=__import__("datetime").timezone.utc),
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {"git_branch": "main", "git_commit": "abc1234"},
    )
    runtime = AppRuntime(
        client=_FakeClient([]),
        service=_FakeService(),
        managed_channels=[],
        observability=observability,
    )

    await runtime.start()
    started_health = json.loads(observability.paths.health_path.read_text(encoding="utf-8"))
    await runtime.stop()

    events = [
        json.loads(line)
        for line in observability.paths.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    health = json.loads(observability.paths.health_path.read_text(encoding="utf-8"))

    assert [event["event"] for event in events] == [
        "bridge.starting",
        "bridge.started",
        "bridge.stopping",
        "bridge.stopped",
    ]
    assert started_health["status"] == "healthy"
    assert started_health["http"]["listening"] is True
    assert health["status"] == "stopped"
    assert health["http"]["listening"] is False


@pytest.mark.asyncio
async def test_app_runtime_emits_start_failed_and_stops_observability_on_startup_error() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_FailingClient(calls),
        service=_FakeService(),
        managed_channels=[],
        observability=_FakeObservability(calls),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await runtime.start()

    assert calls == [
        "obs.start",
        "obs.event:bridge:bridge.starting",
        "client.add_notification_handler",
        "client.add_server_request_handler",
        "client.connect",
        "obs.health",
        "obs.event:bridge:bridge.start_failed",
        "obs.stop",
    ]

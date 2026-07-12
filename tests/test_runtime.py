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

    def add_connection_ready_handler(self, _handler) -> None:
        self.calls.append("client.add_connection_ready_handler")

    async def initialize(self) -> None:
        self.calls.append("client.initialize")

    async def close(self) -> None:
        self.calls.append("client.close")


class _FakeService:
    def handle_notification(self, *_args, **_kwargs) -> None:
        return None

    async def handle_server_request(self, *_args, **_kwargs) -> None:
        return None

    async def handle_connection_ready(self, *_args, **_kwargs) -> None:
        return None


class _ReadyBackend:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def ensure_default_permission_mode(self, _connection_epoch: int) -> None:
        self.calls.append("backend.ensure_default_permission_mode")


class _ReadyService(_FakeService):
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.backend = _ReadyBackend(calls)

    async def handle_connection_ready(self, *_args, **_kwargs) -> None:
        self.calls.append("service.handle_connection_ready")


class _InvokingReadyClient(_FakeClient):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls)
        self.ready_handlers = []

    def add_connection_ready_handler(self, handler) -> None:
        self.calls.append(f"client.add_ready:{handler.__name__}")
        self.ready_handlers.append(handler)

    async def initialize(self) -> None:
        self.calls.append("client.initialize")
        for handler in self.ready_handlers:
            await handler(1)


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
    async def initialize(self) -> None:
        self.calls.append("client.initialize")
        raise RuntimeError("boom")


class _NamedChannel:
    def __init__(self, name: str, calls: list[str], *, fail_start: bool = False, fail_stop: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    async def start(self) -> None:
        self.calls.append(f"{self.name}.start")
        if self.fail_start:
            raise RuntimeError(f"{self.name} start failed")

    async def stop(self) -> None:
        self.calls.append(f"{self.name}.stop")
        if self.fail_stop:
            raise RuntimeError(f"{self.name} stop failed")


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
        "client.add_connection_ready_handler",
        "client.initialize",
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


@pytest.mark.asyncio
async def test_app_runtime_initializes_native_permission_default_before_thread_rehydration() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_InvokingReadyClient(calls),
        service=_ReadyService(calls),
        managed_channels=[_FakeChannel(calls)],
    )

    await runtime.start()

    assert calls == [
        "client.add_notification_handler",
        "client.add_server_request_handler",
        "client.add_ready:ensure_default_permission_mode",
        "client.add_ready:handle_connection_ready",
        "client.initialize",
        "backend.ensure_default_permission_mode",
        "service.handle_connection_ready",
        "channel.start",
    ]


def test_build_runtime_constructs_observability_runtime(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".imcodex",
        run_dir=tmp_path / ".imcodex-run",
        codex_bin="codex",
        app_server_url=None,
        app_server_experimental_api_enabled=False,
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:8765",
        restart_executor=None,
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
        qq_markdown_enabled=False,
    )

    runtime = build_runtime(settings)

    assert runtime.observability is not None
    assert runtime.observability.run_root == settings.run_dir
    assert runtime.observability.service_name == settings.service_name
    assert runtime.client._supervisor.core_mode == "dedicated-ws"
    assert runtime.client._supervisor.core_url == "ws://127.0.0.1:8765"
    assert runtime.client._supervisor.websocket_retry_policy.attempts == settings.app_server_connect_max_attempts
    assert runtime.client._experimental_api_enabled is False


@pytest.mark.asyncio
async def test_app_runtime_persists_launch_snapshot_for_restart_executor(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".imcodex",
        run_dir=tmp_path / ".imcodex-run",
        codex_bin="codex",
        app_server_url=None,
        app_server_experimental_api_enabled=False,
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:8765",
        restart_executor="python -m imcodex ops restart",
        debug_api_enabled=False,
        log_level="INFO",
        http_host="127.0.0.1",
        http_port=8000,
        outbound_url=None,
        service_name="imcodex",
        qq_enabled=False,
        qq_app_id="",
        qq_client_secret="do-not-persist",
        qq_api_base="https://api.sgroup.qq.com",
        qq_markdown_enabled=True,
    )
    runtime = build_runtime(settings)
    runtime.client.initialize = lambda: __import__("asyncio").sleep(0)
    runtime.client.close = lambda: __import__("asyncio").sleep(0)

    await runtime.start()
    launch = json.loads(runtime.observability.paths.current_launch_path.read_text(encoding="utf-8"))
    await runtime.stop()

    assert launch["command"] == ["python", "-m", "imcodex"]
    assert launch["env"]["IMCODEX_DEBUG_API_ENABLED"] == "0"
    assert launch["env"]["IMCODEX_APP_SERVER_EXPERIMENTAL_API"] == "0"
    assert launch["env"]["IMCODEX_CORE_MODE"] == "dedicated-ws"
    assert launch["env"]["IMCODEX_CORE_URL"] == "ws://127.0.0.1:8765"
    assert launch["env"]["IMCODEX_APP_SERVER_AUTH_TOKEN_FILE"] == ""
    assert "IMCODEX_APP_SERVER_AUTH_TOKEN" not in launch["env"]
    assert "IMCODEX_QQ_CLIENT_SECRET" not in launch["env"]
    assert launch["env"]["IMCODEX_QQ_MARKDOWN_ENABLED"] == "1"
    assert launch["port"] == 8000


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
        "client.add_connection_ready_handler",
        "client.initialize",
        "obs.health",
        "obs.event:bridge:bridge.start_failed",
        "obs.stop",
    ]


@pytest.mark.asyncio
async def test_app_runtime_rolls_back_started_channels_and_client_when_channel_start_fails() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[
            _NamedChannel("first", calls),
            _NamedChannel("second", calls, fail_start=True),
        ],
    )

    with pytest.raises(RuntimeError, match="second start failed"):
        await runtime.start()

    assert calls[-4:] == ["first.start", "second.start", "first.stop", "client.close"]


@pytest.mark.asyncio
async def test_app_runtime_stops_remaining_resources_after_channel_stop_failure() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[
            _NamedChannel("first", calls),
            _NamedChannel("second", calls, fail_stop=True),
        ],
    )

    with pytest.raises(ExceptionGroup, match="runtime shutdown failed"):
        await runtime.stop()

    assert calls == ["second.stop", "first.stop", "client.close"]

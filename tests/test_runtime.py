from __future__ import annotations

import json
import sys
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


class _ClosableService(_FakeService):
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def close(self) -> None:
        self.calls.append("service.close")


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
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
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
        "channel.start",
        "client.initialize",
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
async def test_app_runtime_health_is_degraded_when_channel_access_denies_everyone() -> None:
    calls: list[str] = []
    health_updates: list[dict] = []

    class DenyAllChannel(_FakeChannel):
        inbound_access_ready = False

    class CapturingObservability(_FakeObservability):
        def update_health(self, **changes) -> None:
            health_updates.append(changes)

    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[DenyAllChannel(calls)],
        observability=CapturingObservability(calls),
    )

    await runtime.start()
    await runtime.stop()

    assert health_updates[0] == {"status": "degraded"}
    assert health_updates[-1] == {"status": "stopped"}


@pytest.mark.asyncio
async def test_app_runtime_closes_service_before_app_server_client() -> None:
    calls: list[str] = []
    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_ClosableService(calls),
    )

    await runtime.stop()

    assert calls == ["service.close", "client.close"]


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
        "channel.start",
        "client.initialize",
        "backend.ensure_default_permission_mode",
        "service.handle_connection_ready",
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
        native_thread_tool_host=True,
        app_server_reconnect_initial_delay_s=0.6,
        app_server_reconnect_max_delay_s=45.0,
        app_server_reconnect_jitter_fraction=0.15,
    )

    runtime = build_runtime(settings)

    assert runtime.observability is not None
    assert runtime.observability.run_root == settings.run_dir
    assert runtime.observability.service_name == settings.service_name
    assert runtime.client._supervisor.target.connection_mode == "external"
    assert runtime.client._supervisor.connection_target == "ws://127.0.0.1:8765"
    assert runtime.client._supervisor.websocket_retry_policy.attempts == settings.app_server_connect_max_attempts
    assert runtime.client._experimental_api_enabled is True
    assert runtime.service.native_requests.native_thread_tool_host is True
    assert runtime.client._reconnect_retry_policy.initial_delay_s == 0.6
    assert runtime.client._reconnect_retry_policy.max_delay_s == 45.0
    assert runtime.client._reconnect_retry_policy.jitter_fraction == 0.15
    assert runtime.observability._pending_launch_snapshot["settingsSource"] == "explicit"
    assert runtime.observability._pending_launch_snapshot["restartSupported"] is False


@pytest.mark.asyncio
async def test_app_runtime_persists_launch_snapshot_for_restart_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IMCODEX_QQ_ENABLED", "0")
    monkeypatch.setenv("IMCODEX_QQ_CLIENT_SECRET", "do-not-persist")
    monkeypatch.setenv("IMCODEX_QQ_ALLOWED_USER_IDS", "owner-42")
    monkeypatch.setenv("IMCODEX_APP_SERVER_URL", "ws://127.0.0.1:8765")
    monkeypatch.setenv("IMCODEX_NATIVE_THREAD_TOOL_HOST", "1")
    monkeypatch.setenv("IMCODEX_DOTENV_IMPORTED_KEYS", "IMCODEX_QQ_ENABLED")
    monkeypatch.setenv("IMCODEX_LAUNCHER_RELOADABLE_KEYS", "IMCODEX_APP_SERVER_URL")
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
        native_thread_tool_host=True,
        telegram_bot_token="do-not-persist",
        feishu_app_secret="do-not-persist",
        inbound_webhook_token="do-not-persist",
        outbound_webhook_token="do-not-persist",
        app_server_reconnect_initial_delay_s=0.6,
        app_server_reconnect_max_delay_s=45.0,
        app_server_reconnect_jitter_fraction=0.15,
    )
    runtime = build_runtime(settings, settings_source="environment")
    runtime.client.initialize = lambda: __import__("asyncio").sleep(0)
    runtime.client.close = lambda: __import__("asyncio").sleep(0)

    await runtime.start()
    launch = json.loads(runtime.observability.paths.current_launch_path.read_text(encoding="utf-8"))
    await runtime.stop()

    assert launch["command"] == [sys.executable, "-m", "imcodex"]
    assert launch["env"] == {}
    assert launch["settingsSource"] == "environment"
    assert launch["restartSupported"] is True
    assert launch["host"] == "127.0.0.1"
    assert launch["instanceId"] == runtime.observability.context.instance_id
    assert "IMCODEX_HTTP_PORT" in launch["reloadEnvKeys"]
    assert "IMCODEX_QQ_ENABLED" in launch["reloadEnvKeys"]
    assert launch["dotenvImportedKeys"] == ["IMCODEX_QQ_ENABLED"]
    assert launch["launcherReloadableKeys"] == ["IMCODEX_APP_SERVER_URL"]
    required_external = set(launch["requiredExternalEnvKeys"])
    assert {
        "IMCODEX_QQ_ALLOWED_USER_IDS",
        "IMCODEX_QQ_CLIENT_SECRET",
        "IMCODEX_NATIVE_THREAD_TOOL_HOST",
        "PATH",
    } <= required_external
    assert "do-not-persist" not in json.dumps(launch)
    assert launch["port"] == 8000


@pytest.mark.asyncio
async def test_app_runtime_persists_lifecycle_events_and_health_snapshot(
    tmp_path: Path,
) -> None:
    observability = ObservabilityRuntime(
        run_root=tmp_path,
        service_name="imcodex",
        log_level="INFO",
        http_host="127.0.0.1",
        http_port=8000,
        app_server_url=None,
        cwd=Path(r"D:\desktop\imcodex"),
        clock=lambda: __import__("datetime").datetime(
            2026, 4, 19, 10, 15, 30, tzinfo=__import__("datetime").timezone.utc
        ),
        pid_provider=lambda: 48648,
        git_metadata_provider=lambda cwd: {
            "git_branch": "main",
            "git_commit": "abc1234",
        },
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
        "client.close",
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
            _NamedChannel("third", calls),
        ],
    )

    with pytest.raises(RuntimeError, match="second start failed"):
        await runtime.start()

    assert "third.start" not in calls
    assert calls[-6:] == [
        "first.start",
        "second.start",
        "third.stop",
        "second.stop",
        "first.stop",
        "client.close",
    ]


@pytest.mark.asyncio
async def test_app_runtime_stops_prepared_channels_when_client_initialize_fails() -> None:
    calls: list[str] = []
    duplicate = _NamedChannel("duplicate", calls)
    runtime = AppRuntime(
        client=_FailingClient(calls),
        service=_FakeService(),
        managed_channels=[
            _NamedChannel("first", calls),
            duplicate,
            duplicate,
        ],
    )

    with pytest.raises(RuntimeError, match="boom"):
        await runtime.start()

    assert calls.count("first.start") == 1
    assert calls.count("duplicate.start") == 1
    assert calls.count("duplicate.stop") == 1
    assert calls[-3:] == ["duplicate.stop", "first.stop", "client.close"]


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


@pytest.mark.asyncio
async def test_app_runtime_closes_client_when_initialize_is_cancelled() -> None:
    calls: list[str] = []
    initialize_started = __import__("asyncio").Event()

    class BlockingClient(_FakeClient):
        async def initialize(self) -> None:
            self.calls.append("client.initialize")
            initialize_started.set()
            await __import__("asyncio").Event().wait()

    runtime = AppRuntime(client=BlockingClient(calls), service=_FakeService())
    task = __import__("asyncio").create_task(runtime.start())
    await initialize_started.wait()
    task.cancel()

    with pytest.raises(__import__("asyncio").CancelledError):
        await task

    assert calls[-2:] == ["client.initialize", "client.close"]


@pytest.mark.asyncio
async def test_app_runtime_stops_partially_started_channel_when_start_is_cancelled() -> None:
    calls: list[str] = []
    channel_started = __import__("asyncio").Event()

    class BlockingChannel(_FakeChannel):
        async def start(self) -> None:
            self.calls.append("channel.start")
            channel_started.set()
            await __import__("asyncio").Event().wait()

    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[BlockingChannel(calls)],
    )
    task = __import__("asyncio").create_task(runtime.start())
    await channel_started.wait()
    task.cancel()

    with pytest.raises(__import__("asyncio").CancelledError):
        await task

    assert calls[-3:] == ["channel.start", "channel.stop", "client.close"]


@pytest.mark.asyncio
async def test_app_runtime_finishes_shutdown_before_propagating_cancellation() -> None:
    calls: list[str] = []
    stop_started = __import__("asyncio").Event()
    release_stop = __import__("asyncio").Event()

    class BlockingStopChannel(_FakeChannel):
        async def stop(self) -> None:
            self.calls.append("channel.stop")
            stop_started.set()
            await release_stop.wait()

    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[BlockingStopChannel(calls)],
    )
    task = __import__("asyncio").create_task(runtime.stop())
    await stop_started.wait()
    task.cancel()
    await __import__("asyncio").sleep(0)
    assert not task.done()

    release_stop.set()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task

    assert calls == ["channel.stop", "client.close"]


@pytest.mark.asyncio
async def test_app_runtime_ignores_repeated_cancellation_until_shutdown_finishes() -> None:
    calls: list[str] = []
    stop_started = __import__("asyncio").Event()
    release_stop = __import__("asyncio").Event()

    class BlockingStopChannel(_FakeChannel):
        def __init__(self, calls: list[str]) -> None:
            super().__init__(calls)
            self.closed = False

        async def stop(self) -> None:
            self.calls.append("channel.stop.begin")
            stop_started.set()
            await release_stop.wait()
            self.closed = True
            self.calls.append("channel.stop.end")

    channel = BlockingStopChannel(calls)
    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[channel],
    )
    task = __import__("asyncio").create_task(runtime.stop())
    await stop_started.wait()

    task.cancel()
    await __import__("asyncio").sleep(0)
    task.cancel()
    await __import__("asyncio").sleep(0)

    assert not task.done()
    assert channel.closed is False
    release_stop.set()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task

    assert channel.closed is True
    assert calls == ["channel.stop.begin", "channel.stop.end", "client.close"]


@pytest.mark.asyncio
async def test_observability_failures_never_skip_runtime_resource_cleanup() -> None:
    calls: list[str] = []

    class FailingObservability(_FakeObservability):
        def emit_event(self, *, component: str, event: str, **_kwargs) -> None:
            self.calls.append(f"obs.event:{component}:{event}")
            if event == "bridge.stopping":
                raise OSError("log disk full")

        def update_health(self, **_kwargs) -> None:
            self.calls.append("obs.health")
            raise OSError("health disk full")

    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        managed_channels=[_FakeChannel(calls)],
        observability=FailingObservability(calls),
    )

    await runtime.start()
    await runtime.stop()

    assert "channel.stop" in calls
    assert "client.close" in calls
    assert calls[-1] == "obs.stop"


@pytest.mark.asyncio
async def test_observability_shutdown_never_blocks_event_loop() -> None:
    calls: list[str] = []
    stop_started = __import__("threading").Event()
    release_stop = __import__("threading").Event()

    class BlockingObservability(_FakeObservability):
        def stop(self) -> None:
            self.calls.append("obs.stop.begin")
            stop_started.set()
            release_stop.wait(timeout=1)
            self.calls.append("obs.stop.end")

    runtime = AppRuntime(
        client=_FakeClient(calls),
        service=_FakeService(),
        observability=BlockingObservability(calls),
    )
    shutdown = __import__("asyncio").create_task(runtime.stop())
    assert await __import__("asyncio").to_thread(stop_started.wait, 1)
    ticker_fired = __import__("asyncio").Event()

    async def tick() -> None:
        await __import__("asyncio").sleep(0.01)
        ticker_fired.set()

    ticker = __import__("asyncio").create_task(tick())
    await __import__("asyncio").wait_for(ticker_fired.wait(), timeout=0.2)
    release_stop.set()
    await shutdown
    await ticker

    assert calls[-2:] == ["obs.stop.begin", "obs.stop.end"]

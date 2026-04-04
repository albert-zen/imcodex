from __future__ import annotations

import pytest

from imcodex.runtime import AppRuntime


class FakeSupervisor:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakeClient:
    def __init__(self) -> None:
        self.connected = 0
        self.initialized = 0
        self.closed = 0
        self.notification_handlers = []
        self.server_request_handlers = []

    async def connect(self) -> None:
        self.connected += 1

    async def initialize(self):
        self.initialized += 1
        return {"ok": True}

    async def close(self) -> None:
        self.closed += 1

    def add_notification_handler(self, handler) -> None:
        self.notification_handlers.append(handler)

    def add_server_request_handler(self, handler) -> None:
        self.server_request_handlers.append(handler)


class FakeService:
    def __init__(self) -> None:
        self.store = FakeStore()

    async def handle_notification(self, payload):
        return [payload]

    async def handle_server_request(self, payload):
        return [payload]


class FakeStore:
    def __init__(self) -> None:
        self.cleared = 0

    def clear_stale_active_turns(self) -> int:
        self.cleared += 1
        return 1


class FakeChannel:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.mark.asyncio
async def test_runtime_start_and_stop_wire_handlers() -> None:
    channel = FakeChannel()
    runtime = AppRuntime(
        supervisor=FakeSupervisor(),
        client=FakeClient(),
        service=FakeService(),
        managed_channels=[channel],
    )

    await runtime.start()
    await runtime.stop()

    assert runtime.supervisor.started == 1
    assert runtime.client.connected == 0
    assert runtime.client.initialized == 0
    assert len(runtime.client.notification_handlers) == 1
    assert len(runtime.client.server_request_handlers) == 1
    assert runtime.service.store.cleared == 1
    assert channel.started == 1
    assert channel.stopped == 1
    assert runtime.supervisor.stopped == 1
    assert runtime.client.closed == 1

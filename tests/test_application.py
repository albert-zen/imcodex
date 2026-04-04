from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from imcodex.application import build_runtime, create_application, open_blocking_websocket
from imcodex.config import Settings
from imcodex.outbound import MultiplexOutboundSink


class FakeService:
    async def handle_inbound(self, message):
        return []


class FakeRuntime:
    def __init__(self) -> None:
        self.service = FakeService()
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def test_application_lifespan_starts_and_stops_runtime() -> None:
    runtime = FakeRuntime()

    with TestClient(create_application(runtime=runtime)) as client:
        response = client.post(
            "/api/channels/webhook/inbound",
            json={
                "channel_id": "demo",
                "conversation_id": "conv-1",
                "user_id": "u1",
                "message_id": "m1",
                "text": "/status",
            },
        )
        assert response.status_code == 200
        assert runtime.started == 1

    assert runtime.stopped == 1


def test_build_runtime_attaches_qq_channel_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        codex_bin="codex",
        app_server_host="127.0.0.1",
        app_server_port=8765,
        outbound_url=None,
        service_name="imcodex",
        qq_enabled=True,
        qq_app_id="app-id",
        qq_client_secret="secret",
        qq_api_base="https://api.sgroup.qq.com",
    )

    runtime = build_runtime(settings)

    assert len(runtime.managed_channels) == 1
    assert runtime.service.outbound_sink is not None
    assert isinstance(runtime.service.outbound_sink, MultiplexOutboundSink)


def test_open_blocking_websocket_clears_read_timeout(monkeypatch) -> None:
    seen = {}

    class FakeSocket:
        def __init__(self) -> None:
            self.timeout = "unset"

        def settimeout(self, value) -> None:
            self.timeout = value

    fake_socket = FakeSocket()

    def fake_create_connection(url, timeout, suppress_origin):
        seen["url"] = url
        seen["timeout"] = timeout
        seen["suppress_origin"] = suppress_origin
        return fake_socket

    monkeypatch.setattr("imcodex.application.websocket.create_connection", fake_create_connection)

    ws = open_blocking_websocket("ws://127.0.0.1:8765")

    assert ws is fake_socket
    assert fake_socket.timeout is None
    assert seen == {
        "url": "ws://127.0.0.1:8765",
        "timeout": 10,
        "suppress_origin": True,
    }

from __future__ import annotations

import asyncio
from pathlib import Path

from imcodex.channels import MultiplexOutboundSink
from imcodex.composition import build_runtime, open_blocking_websocket
from imcodex.config import Settings


def test_build_runtime_attaches_qq_channel_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        codex_bin="codex",
        http_host="0.0.0.0",
        http_port=8000,
        app_server_host="127.0.0.1",
        app_server_port=8765,
        outbound_url=None,
        service_name="imcodex",
        auto_approve=False,
        auto_approve_mode="session",
        qq_enabled=True,
        qq_app_id="app-id",
        qq_client_secret="secret",
        qq_api_base="https://api.sgroup.qq.com",
    )

    runtime = build_runtime(settings)

    assert len(runtime.managed_channels) == 1
    assert runtime.service.outbound_sink is not None
    assert isinstance(runtime.service.outbound_sink, MultiplexOutboundSink)
    assert runtime.service.session_registry is not None
    assert runtime.service.thread_directory is not None
    assert runtime.service.request_registry is not None
    assert runtime.service.turn_state is not None


def test_open_blocking_websocket_uses_async_websockets_client(monkeypatch) -> None:
    seen = {}

    async def fake_connect(url, open_timeout):
        seen["url"] = url
        seen["open_timeout"] = open_timeout
        return "fake-ws"

    monkeypatch.setattr("imcodex.composition.websockets.connect", fake_connect)

    ws = asyncio.run(open_blocking_websocket("ws://127.0.0.1:8765"))

    assert ws == "fake-ws"
    assert seen == {
        "url": "ws://127.0.0.1:8765",
        "open_timeout": 10,
    }

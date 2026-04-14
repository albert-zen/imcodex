from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest

from imcodex.appserver import AppServerClient, CodexBackend
from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.channels import MultiplexOutboundSink, QQChannelAdapter, TOKEN_URL
from imcodex.store import ConversationStore


@dataclass
class ScriptedCodexWebSocket:
    sent: list[str]
    incoming: asyncio.Queue[str]
    scripts: dict[int, list[str]]
    closed: bool = False

    async def send(self, message: str) -> None:
        self.sent.append(message)
        payload = json.loads(message)
        request_id = payload.get("id")
        if isinstance(request_id, int):
            for item in self.scripts.get(request_id, []):
                self.incoming.put_nowait(item)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


class FakeQQWebSocket:
    def __init__(self, messages: list[str], stop_event: asyncio.Event) -> None:
        self._messages = list(messages)
        self._stop_event = stop_event
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        await self._stop_event.wait()
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


@pytest.mark.asyncio
async def test_mock_e2e_qq_dispatch_reaches_final_reply() -> None:
    codex_incoming: asyncio.Queue[str] = asyncio.Queue()
    codex_incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    codex_ws = ScriptedCodexWebSocket(
        sent=[],
        incoming=codex_incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_qq_1","preview":"hi"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_qq_1","status":"inProgress"}}}',
                '{"method":"item/agentMessage/delta","params":{"threadId":"thr_qq_1","turnId":"turn_qq_1","itemId":"item_1","delta":"Hello"}}',
                '{"method":"item/completed","params":{"threadId":"thr_qq_1","turnId":"turn_qq_1","item":{"id":"item_1","type":"agentMessage","text":"Hello from Codex over QQ"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_qq_1","turn":{"id":"turn_qq_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: codex_ws,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_selected_cwd("qq", "c2c:user-1", r"D:\work\alpha")
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name="imcodex-test"),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=None,
    )
    client.add_notification_handler(service.handle_notification)

    outbound_requests: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "qq-token", "expires_in": 7200}, request=request)
        if url == "https://sandbox.api.sgroup.qq.com/gateway":
            return httpx.Response(
                200,
                json={"url": "wss://sandbox.api.sgroup.qq.com/websocket"},
                request=request,
            )
        if url == "https://sandbox.api.sgroup.qq.com/v2/users/user-1/messages":
            outbound_requests.append((url, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"id": f"ok-{len(outbound_requests)}"}, request=request)
        raise AssertionError(f"unexpected request {url}")

    qq_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qq_messages = [
        json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}),
        json.dumps({"op": 0, "t": "READY", "d": {"session_id": "qq-session-1"}}),
        json.dumps(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "msg-1",
                    "content": "hi",
                    "author": {"user_openid": "user-1"},
                },
            }
        ),
    ]
    qq_adapter = QQChannelAdapter(
        enabled=True,
        app_id="1903391685",
        client_secret="secret",
        service=service,
        api_base="https://sandbox.api.sgroup.qq.com",
        http_client=qq_client,
        websocket_factory=lambda _: FakeQQWebSocket(qq_messages, asyncio.Event()),
    )
    service.outbound_sink = MultiplexOutboundSink(channel_sinks={"qq": qq_adapter})

    await client.connect()
    await client.initialize()
    await qq_adapter.start()
    for _ in range(50):
        if len(outbound_requests) >= 2 and any('"method": "turn/start"' in item for item in codex_ws.sent):
            break
        await asyncio.sleep(0.01)

    assert any('"method": "thread/start"' in item for item in codex_ws.sent)
    assert any('"method": "turn/start"' in item for item in codex_ws.sent)
    assert len(outbound_requests) == 2
    contents = [payload["content"] for _, payload in outbound_requests]
    assert "[System] Accepted. Processing started." in contents
    assert any("Hello from Codex over QQ" in content for content in contents)
    assert [payload.get("msg_id") for _, payload in outbound_requests] == ["msg-1", "msg-1"]

    await qq_adapter.stop()
    await client.close()
    await qq_client.aclose()


@pytest.mark.asyncio
async def test_mock_e2e_qq_contract_emits_ack_progress_then_final() -> None:
    codex_incoming: asyncio.Queue[str] = asyncio.Queue()
    codex_incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    codex_ws = ScriptedCodexWebSocket(
        sent=[],
        incoming=codex_incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_qq_1","preview":"hi"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_qq_1","status":"inProgress"}}}',
                '{"method":"item/completed","params":{"threadId":"thr_qq_1","turnId":"turn_qq_1","item":{"id":"item_progress","type":"agentMessage","phase":"draft","text":"Checking the repo structure."}}}',
                '{"method":"item/completed","params":{"threadId":"thr_qq_1","turnId":"turn_qq_1","item":{"id":"item_final","type":"agentMessage","phase":"final_answer","text":"Hello from Codex over QQ"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_qq_1","turn":{"id":"turn_qq_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: codex_ws,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_selected_cwd("qq", "c2c:user-1", r"D:\work\alpha")
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name="imcodex-test"),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=None,
    )
    client.add_notification_handler(service.handle_notification)

    outbound_requests: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "qq-token", "expires_in": 7200}, request=request)
        if url == "https://sandbox.api.sgroup.qq.com/gateway":
            return httpx.Response(
                200,
                json={"url": "wss://sandbox.api.sgroup.qq.com/websocket"},
                request=request,
            )
        if url == "https://sandbox.api.sgroup.qq.com/v2/users/user-1/messages":
            outbound_requests.append((url, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"id": f"ok-{len(outbound_requests)}"}, request=request)
        raise AssertionError(f"unexpected request {url}")

    qq_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qq_messages = [
        json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}),
        json.dumps({"op": 0, "t": "READY", "d": {"session_id": "qq-session-1"}}),
        json.dumps(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "msg-1",
                    "content": "hi",
                    "author": {"user_openid": "user-1"},
                },
            }
        ),
    ]
    qq_adapter = QQChannelAdapter(
        enabled=True,
        app_id="1903391685",
        client_secret="secret",
        service=service,
        api_base="https://sandbox.api.sgroup.qq.com",
        http_client=qq_client,
        websocket_factory=lambda _: FakeQQWebSocket(qq_messages, asyncio.Event()),
    )
    service.outbound_sink = MultiplexOutboundSink(channel_sinks={"qq": qq_adapter})

    await client.connect()
    await client.initialize()
    await qq_adapter.start()
    for _ in range(50):
        if len(outbound_requests) >= 3:
            break
        await asyncio.sleep(0.01)

    contents = [payload["content"] for _, payload in outbound_requests]
    assert len(contents) == 3
    assert "[System] Accepted. Processing started." in contents
    assert "Checking the repo structure." in contents
    assert "Hello from Codex over QQ" in contents

    await qq_adapter.stop()
    await client.close()
    await qq_client.aclose()

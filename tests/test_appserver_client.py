import asyncio
from dataclasses import dataclass
import json
import queue

import pytest

from imcodex.appserver_client import AppServerClient, AppServerError


@dataclass
class ScriptedWebSocket:
    sent: list
    incoming: asyncio.Queue
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


class FlakyWebSocket(ScriptedWebSocket):
    def __init__(self, *, sent, incoming, scripts, fail_after_recvs: int) -> None:
        super().__init__(sent=sent, incoming=incoming, scripts=scripts)
        self.fail_after_recvs = fail_after_recvs
        self.recv_count = 0

    async def recv(self) -> str:
        self.recv_count += 1
        if self.recv_count > self.fail_after_recvs and len(self.sent) >= 2:
            raise RuntimeError("socket dropped")
        return await super().recv()


class SyncScriptedWebSocket:
    def __init__(self, *, sent, incoming, scripts) -> None:
        self.sent = sent
        self.incoming = incoming
        self.scripts = scripts
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)
        payload = json.loads(message)
        request_id = payload.get("id")
        if isinstance(request_id, int):
            for item in self.scripts.get(request_id, []):
                self.incoming.put(item)

    def recv(self) -> str:
        return self.incoming.get(timeout=1)

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_initialize_sends_initialize_and_initialized_notification():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(sent=[], incoming=incoming, scripts={})
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    response = await client.initialize()

    assert response["ok"] is True
    assert client.initialized is True
    assert len(websocket.sent) == 2
    assert '"method": "initialize"' in websocket.sent[0]
    assert '"method": "initialized"' in websocket.sent[1]
    await client.close()


@pytest.mark.asyncio
async def test_client_supports_sync_websocket_factory():
    incoming = queue.Queue()
    incoming.put('{"id":1,"result":{"ok":true}}')
    websocket = SyncScriptedWebSocket(sent=[], incoming=incoming, scripts={})
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    response = await client.initialize()

    assert response["ok"] is True
    assert len(websocket.sent) == 2
    assert websocket.closed is False
    await client.close()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_turn_request_correlation_and_notifications():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"turn/started","params":{"turn":{"id":"turn_1","status":"inProgress"},"threadId":"thr_1"}}',
                '{"method":"item/agentMessage/delta","params":{"threadId":"thr_1","turnId":"turn_1","itemId":"item_1","delta":"Hello"}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_1","type":"agentMessage","text":"Hello world"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_1","turn":{"id":"turn_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    events = []
    client.add_notification_handler(events.append)
    result = await client.start_thread(cwd="D:/desktop/project")
    turn = await client.start_turn(thread_id=result["thread"]["id"], text="hello")

    assert result["thread"]["id"] == "thr_1"
    assert turn["turn"]["id"] == "turn_1"
    assert any(e["method"] == "turn/started" for e in events)
    assert any(e["method"] == "item/agentMessage/delta" for e in events)
    assert any(e["method"] == "turn/completed" for e in events)
    assert client.last_agent_message == "Hello world"
    await client.close()


@pytest.mark.asyncio
async def test_async_notification_handlers_are_awaited():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: [
                '{"id":2,"result":{"thread":{"id":"thr_1"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_1","turn":{"id":"turn_1","status":"completed"}}}',
            ]
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    seen = []

    async def capture(notification):
        seen.append(notification["method"])

    await client.connect()
    await client.initialize()
    client.add_notification_handler(capture)
    await client.start_thread(cwd="D:/desktop/project")
    await asyncio.sleep(0)

    assert seen == ["turn/completed"]
    await client.close()


@pytest.mark.asyncio
async def test_server_initiated_requests_are_buffered():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"id":99,"method":"item/tool/requestUserInput","params":{"threadId":"thr_1","turnId":"turn_1","questionId":"q1","prompt":"What time?"}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    captured = []
    client.add_server_request_handler(captured.append)
    await client.start_thread(cwd="D:/desktop/project")
    await client.start_turn(thread_id="thr_1", text="hello")
    await asyncio.sleep(0)

    pending = client.pending_requests()
    assert pending[0]["method"] == "item/tool/requestUserInput"
    assert captured[0]["params"]["_request_id"] == "99"
    assert pending[0]["params"]["questionId"] == "q1"
    await client.close()


@pytest.mark.asyncio
async def test_reply_to_pending_request_tracks_decision():
    client = AppServerClient(
        websocket_factory=lambda _: None,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    websocket = ScriptedWebSocket(sent=[], incoming=asyncio.Queue(), scripts={})
    client._ws = websocket
    client._pending_requests["ticket-1"] = {"id": 10, "method": "item/fileChange/requestApproval"}

    payload = await client.reply_to_server_request("ticket-1", {"decision": "accept"})

    assert payload["id"] == 10
    assert payload["result"] == {"decision": "accept"}
    assert "ticket-1" not in client._pending_requests
    assert '"decision": "accept"' in websocket.sent[0]


@pytest.mark.asyncio
async def test_unknown_ticket_raises():
    client = AppServerClient(
        websocket_factory=lambda _: None,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    with pytest.raises(AppServerError):
        await client.reply_to_server_request("missing", {"decision": "accept"})


@pytest.mark.asyncio
async def test_client_reconnects_on_next_request_after_receive_loop_failure():
    connections = []

    def websocket_factory(_: str):
        index = len(connections)
        incoming = asyncio.Queue()
        if index == 0:
            incoming.put_nowait('{"id":1,"result":{"ok":true}}')
            ws = FlakyWebSocket(sent=[], incoming=incoming, scripts={}, fail_after_recvs=1)
        else:
            ws = ScriptedWebSocket(
                sent=[],
                incoming=incoming,
                scripts={2: ['{"id":2,"result":{"thread":{"id":"thr_2"}}}']},
            )
        connections.append(ws)
        return ws

    client = AppServerClient(
        websocket_factory=websocket_factory,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    await asyncio.sleep(0)

    result = await client.start_thread(cwd="D:/desktop/project")

    assert result["thread"]["id"] == "thr_2"
    assert len(connections) == 2
    await client.close()

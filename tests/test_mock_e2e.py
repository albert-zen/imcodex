from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from imcodex.appserver import AppServerClient, CodexBackend
from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


@dataclass
class ScriptedWebSocket:
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


class CapturingSink:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def send_message(self, message: OutboundMessage) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_mock_e2e_text_turn_streams_final_reply_to_outbound_sink() -> None:
    incoming: asyncio.Queue[str] = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1","preview":"repo help"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"item/agentMessage/delta","params":{"threadId":"thr_1","turnId":"turn_1","itemId":"item_1","delta":"Hello"}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_1","type":"agentMessage","text":"Hello from Codex"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_1","turn":{"id":"turn_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)
    sink = CapturingSink()
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name="imcodex-test"),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=sink,
    )
    client.add_notification_handler(service.handle_notification)

    await client.connect()
    await client.initialize()

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="inspect the repo",
        )
    )
    await asyncio.sleep(0)

    assert [message.message_type for message in messages] == ["accepted"]
    assert messages[0].text == "Working on it."
    assert websocket.closed is False
    assert any('"method": "thread/start"' in item for item in websocket.sent)
    assert any('"method": "turn/start"' in item for item in websocket.sent)
    assert len(sink.messages) == 1
    assert sink.messages[0].channel_id == "qq"
    assert sink.messages[0].conversation_id == "conv-1"
    assert sink.messages[0].message_type == "turn_result"
    assert "Hello from Codex" in sink.messages[0].text

    await client.close()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_mock_e2e_attaches_external_thread_and_continues_on_it() -> None:
    incoming: asyncio.Queue[str] = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_external","preview":"Imported thread"}}}'],
            3: ['{"id":3,"result":{"thread":{"id":"thr_external","preview":"Imported thread"}}}'],
            4: [
                '{"id":4,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"item/completed","params":{"threadId":"thr_external","turnId":"turn_1","item":{"id":"item_1","type":"agentMessage","text":"Continuing attached thread"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_external","turn":{"id":"turn_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)
    sink = CapturingSink()
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name="imcodex-test"),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=sink,
    )
    client.add_notification_handler(service.handle_notification)

    await client.connect()
    await client.initialize()

    attach_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/thread attach thr_external",
        )
    )
    turn_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="continue from there",
        )
    )
    await asyncio.sleep(0)

    assert attach_messages[0].text == "Attached thread Imported thread (id: thr_external)."
    assert turn_messages[0].text == "Working on it."
    assert sum('"method": "thread/resume"' in item for item in websocket.sent) == 2
    assert any('"threadId": "thr_external"' in item for item in websocket.sent if '"method": "turn/start"' in item)
    assert sink.messages[-1].message_type == "turn_result"
    assert "Continuing attached thread" in sink.messages[-1].text

    await client.close()

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
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
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
    assert messages[0].text == "[System] Accepted. Processing started."
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
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
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

    assert attach_messages[0].text == "[System] Attached to thread Imported thread (id: thr_external)."
    assert turn_messages[0].text == "[System] Accepted. Processing started."
    assert sum('"method": "thread/read"' in item for item in websocket.sent) == 1
    assert sum('"method": "thread/resume"' in item for item in websocket.sent) == 1
    assert any('"threadId": "thr_external"' in item for item in websocket.sent if '"method": "turn/start"' in item)
    assert sink.messages[-1].message_type == "turn_result"
    assert "Continuing attached thread" in sink.messages[-1].text

    await client.close()


@pytest.mark.asyncio
async def test_mock_e2e_restart_reuses_attached_native_thread(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    incoming: asyncio.Queue[str] = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_external","preview":"Imported thread","cwd":"D:\\\\work\\\\alpha"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
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

    assert attach_messages[0].text == "[System] Attached to thread Imported thread (id: thr_external)."

    await client.close()

    restarted_incoming: asyncio.Queue[str] = asyncio.Queue()
    restarted_incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    restarted_socket = ScriptedWebSocket(
        sent=[],
        incoming=restarted_incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_external","preview":"Imported thread","cwd":"D:\\\\work\\\\alpha"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"item/completed","params":{"threadId":"thr_external","turnId":"turn_1","item":{"id":"item_1","type":"agentMessage","text":"Still on the same native thread"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_external","turn":{"id":"turn_1","status":"completed"}}}',
            ],
        },
    )
    restarted_client = AppServerClient(
        websocket_factory=lambda _: restarted_socket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    restarted_store = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    restarted_sink = CapturingSink()
    restarted_service = BridgeService(
        store=restarted_store,
        backend=CodexBackend(
            client=restarted_client,
            store=restarted_store,
            service_name="imcodex-test",
        ),
        command_router=CommandRouter(restarted_store),
        projector=MessageProjector(),
        outbound_sink=restarted_sink,
    )
    restarted_client.add_notification_handler(restarted_service.handle_notification)

    await restarted_client.connect()
    await restarted_client.initialize()

    turn_messages = await restarted_service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="continue from there after restart",
        )
    )
    await asyncio.sleep(0)

    assert turn_messages[0].text == "[System] Accepted. Processing started."
    assert sum('"method": "thread/resume"' in item for item in restarted_socket.sent) == 1
    assert not any('"method": "thread/start"' in item for item in restarted_socket.sent)
    assert any('"threadId": "thr_external"' in item for item in restarted_socket.sent if '"method": "turn/start"' in item)
    assert restarted_sink.messages[-1].message_type == "turn_result"
    assert "Still on the same native thread" in restarted_sink.messages[-1].text

    await restarted_client.close()


@pytest.mark.asyncio
async def test_mock_e2e_sync_ack_then_async_progress_then_final_result() -> None:
    incoming: asyncio.Queue[str] = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1","preview":"repo help"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_progress","type":"agentMessage","phase":"draft","text":"Checking the repo structure."}}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_final","type":"agentMessage","phase":"final_answer","text":"Here is the final answer."}}}',
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
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
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
    assert [message.message_type for message in sink.messages] == ["turn_progress", "turn_result"]
    assert sink.messages[0].text == "Checking the repo structure."
    assert sink.messages[1].text == "Here is the final answer."

    await client.close()


@pytest.mark.asyncio
async def test_mock_e2e_late_tool_progress_after_final_answer_is_not_pushed() -> None:
    incoming: asyncio.Queue[str] = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1","preview":"repo help"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_final","type":"agentMessage","phase":"final_answer","text":"Here is the final answer."}}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"cmd_1","type":"commandExecution","command":"pytest -q"}}}',
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
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
    store.set_toolcall_visibility("qq", "conv-1", enabled=True)
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
    assert [message.message_type for message in sink.messages] == ["turn_result"]
    assert sink.messages[0].text == "Here is the final answer."

    await client.close()

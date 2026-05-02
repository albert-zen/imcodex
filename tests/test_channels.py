from __future__ import annotations

import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from imcodex.channels import QQChannelAdapter, create_app
from imcodex.channels.middleware import UnifiedChannelMiddleware
from imcodex.channels.qq import OP_DISPATCH, OP_HELLO, RECONNECT_MAX_DELAY_S
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


class StubService:
    async def handle_inbound(self, message):
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


class CountingService:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store
        self.calls: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage):
        self.calls.append(message)
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def test_webhook_inbound_returns_messages() -> None:
    client = TestClient(create_app(StubService()))

    response = client.post(
        "/api/channels/webhook/inbound",
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["message_type"] == "accepted"


def test_qq_adapter_normalizes_group_mention_message() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    inbound = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "<@123>  inspect repo",
            "group_openid": "group-1",
            "author": {"member_openid": "user-1"},
        },
    )

    assert inbound is not None
    assert inbound.conversation_id == "group:group-1"
    assert inbound.text == "inspect repo"


@pytest.mark.asyncio
async def test_qq_adapter_delegates_standardized_inbound_message_to_middleware() -> None:
    class CapturingMiddleware:
        def __init__(self) -> None:
            self.seen: list[InboundMessage] = []

        async def handle_inbound(self, adapter, inbound, *, reply_to_message_id=None):
            self.seen.append(inbound)
            await adapter.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id=inbound.conversation_id,
                    message_type="turn_result",
                    text="Accepted",
                    metadata={"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {},
                )
            )

    middleware = CapturingMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
    )
    sent: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        sent.append(message)

    adapter.send_message = capture  # type: ignore[method-assign]

    await adapter.handle_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "hello",
            "author": {"user_openid": "user-1"},
        },
    )

    assert sent
    assert middleware.seen
    assert middleware.seen[0].text == "hello"
    assert sent[0].message_type == "turn_result"
    assert sent[0].metadata["reply_to_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_qq_adapter_emits_ready_event_and_health_update(monkeypatch) -> None:
    observed_events: list[dict] = []
    observed_health: list[tuple[str, dict]] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.emit_event", capture_event)
    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self._messages = iter(
                [
                    json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1}}),
                    json.dumps(
                        {
                            "op": OP_DISPATCH,
                            "t": "READY",
                            "d": {"session_id": "session-1"},
                        }
                    ),
                ]
            )

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fast_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        websocket_factory=lambda _url: FakeConnection(FakeWebSocket()),
        sleep=fast_sleep,
    )

    await adapter._run_session("ws://gateway", "token")

    assert [event["event"] for event in observed_events] == ["qq.gateway.ready"]
    assert observed_health == [
        ("qq", {"connected": True, "session_id": "session-1", "status": "connected"})
    ]


@pytest.mark.asyncio
async def test_qq_adapter_start_survives_initial_network_failure(monkeypatch) -> None:
    observed_health: list[tuple[str, dict]] = []

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FailingHttpClient:
        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://bots.qq.com/app/getAppAccessToken")
            raise httpx.ConnectError("network unavailable", request=request)

    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    delays: list[float] = []

    async def controlled_sleep(seconds: float) -> None:
        delays.append(seconds)
        sleep_started.set()
        await release_sleep.wait()

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        http_client=FailingHttpClient(),
        sleep=controlled_sleep,
    )

    await adapter.start()
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert adapter._runner_task is not None
    assert not adapter._runner_task.done()
    assert delays == [1.0]
    assert observed_health[0] == (
        "qq",
        {"enabled": True, "connected": False, "status": "connecting"},
    )
    assert observed_health[-1] == (
        "qq",
        {
            "connected": False,
            "session_id": None,
            "status": "reconnecting",
            "error_type": "ConnectError",
            "retry_delay_s": 1.0,
        },
    )

    await adapter.stop()


def test_qq_adapter_reconnect_delay_is_capped() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    assert adapter._reconnect_delay(0) == 1.0
    assert adapter._reconnect_delay(1) == 1.0
    assert adapter._reconnect_delay(3) == 4.0
    assert adapter._reconnect_delay(100) == RECONNECT_MAX_DELAY_S


@pytest.mark.asyncio
async def test_channel_middleware_drops_short_window_duplicate_inbound_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.channels.middleware.emit_event", capture_event)

    clock = iter([1.0, 1.0, 1.1, 1.1])
    store = ConversationStore(clock=lambda: next(clock))
    service = CountingService(store)
    middleware = UnifiedChannelMiddleware(service=service)

    class FakeAdapter:
        channel_id = "qq"

        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.sent.append(message)

    adapter = FakeAdapter()
    inbound_1 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m1",
        text="Codex help这种命令你觉得会很重吗？",
    )
    inbound_2 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m2",
        text="Codex help这种命令你觉得会很重吗？",
    )

    await middleware.handle_inbound(adapter, inbound_1, reply_to_message_id="m1")
    await middleware.handle_inbound(adapter, inbound_2, reply_to_message_id="m2")

    assert [message.message_type for message in adapter.sent] == ["accepted"]
    assert [message.message_id for message in service.calls] == ["m1"]
    assert [event["event"] for event in observed_events] == [
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
        "message.inbound.received",
        "message.inbound.duplicate_dropped",
    ]

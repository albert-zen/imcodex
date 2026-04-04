from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from imcodex.models import OutboundMessage
from imcodex.qq_adapter import QQChannelAdapter


class FakeService:
    def __init__(self) -> None:
        self.inbound_messages = []

    async def handle_inbound(self, message):
        self.inbound_messages.append(message)
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def make_adapter(**overrides) -> QQChannelAdapter:
    defaults = {
        "enabled": True,
        "app_id": "app-id",
        "client_secret": "secret",
        "service": FakeService(),
        "api_base": "https://api.sgroup.qq.com",
    }
    defaults.update(overrides)
    return QQChannelAdapter(**defaults)


def test_c2c_event_maps_to_inbound_message() -> None:
    adapter = make_adapter()

    inbound = adapter.parse_inbound_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "inspect repo",
            "author": {"user_openid": "user-1"},
        },
    )

    assert inbound is not None
    assert inbound.channel_id == "qq"
    assert inbound.conversation_id == "c2c:user-1"
    assert inbound.user_id == "user-1"
    assert inbound.message_id == "msg-1"
    assert inbound.text == "inspect repo"


def test_group_event_strips_at_prefix_and_uses_group_conversation() -> None:
    adapter = make_adapter()

    inbound = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-2",
            "content": "<@!98765>   review this file",
            "author": {"member_openid": "member-1"},
            "group_openid": "group-1",
        },
    )

    assert inbound is not None
    assert inbound.conversation_id == "group:group-1"
    assert inbound.user_id == "member-1"
    assert inbound.text == "review this file"


@pytest.mark.asyncio
async def test_dispatch_event_calls_service_and_replies_with_reply_context() -> None:
    service = FakeService()
    adapter = make_adapter(service=service)
    sent = []

    async def capture_send(message: OutboundMessage) -> None:
        sent.append(message)

    adapter.send_message = capture_send  # type: ignore[method-assign]

    await adapter.handle_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg-9",
            "content": "hello codex",
            "author": {"user_openid": "user-9"},
        },
    )

    assert len(service.inbound_messages) == 1
    assert service.inbound_messages[0].conversation_id == "c2c:user-9"
    assert len(sent) == 1
    assert sent[0].metadata["reply_to_message_id"] == "msg-9"


@pytest.mark.asyncio
async def test_send_message_posts_c2c_payload() -> None:
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = make_adapter(http_client=client)
    adapter._access_token = "token-1"
    adapter._access_token_expires_at = 9999999999

    await adapter.send_message(
        OutboundMessage(
            channel_id="qq",
            conversation_id="c2c:user-5",
            message_type="turn_result",
            text="done",
            metadata={"reply_to_message_id": "msg-5"},
        )
    )

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.sgroup.qq.com/v2/users/user-5/messages"
    assert requests[0].headers["Authorization"] == "QQBot token-1"
    body = json.loads(requests[0].content.decode("utf-8"))
    assert body["content"] == "done"
    assert body["msg_type"] == 0
    assert body["msg_seq"] == 1
    assert body["msg_id"] == "msg-5"


@pytest.mark.asyncio
async def test_send_message_posts_group_payload() -> None:
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = make_adapter(http_client=client)
    adapter._access_token = "token-2"
    adapter._access_token_expires_at = 9999999999

    await adapter.send_message(
        OutboundMessage(
            channel_id="qq",
            conversation_id="group:group-8",
            message_type="turn_result",
            text="group reply",
        )
    )

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.sgroup.qq.com/v2/groups/group-8/messages"
    body = json.loads(requests[0].content.decode("utf-8"))
    assert body["content"] == "group reply"
    assert body["msg_seq"] == 1


@pytest.mark.asyncio
async def test_send_message_raises_on_http_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = make_adapter(http_client=client)
    adapter._access_token = "token-3"
    adapter._access_token_expires_at = 9999999999

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-7",
                message_type="turn_result",
                text="broken",
            )
        )


@pytest.mark.asyncio
async def test_send_message_raises_on_qq_http_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = make_adapter(http_client=client)
    adapter._access_token = "token-3"
    adapter._access_token_expires_at = 9999999999

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-10",
                message_type="turn_result",
                text="this should fail",
            )
        )


@pytest.mark.asyncio
async def test_run_session_cancels_heartbeat_on_socket_error() -> None:
    cancelled = asyncio.Event()

    class FakeWebSocket:
        def __init__(self) -> None:
            self._step = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._step == 0:
                self._step += 1
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 1}})
            await asyncio.sleep(0)
            raise RuntimeError("socket boom")

        async def send(self, data: str) -> None:
            return None

    adapter = make_adapter(websocket_factory=lambda url: FakeWebSocket())

    async def fake_heartbeat(_websocket, _interval: float) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter._heartbeat_loop = fake_heartbeat  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="socket boom"):
        await adapter._run_session("wss://gateway", "token-4")

    assert cancelled.is_set()

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from imcodex.channels import ChannelAccessPolicy, TelegramAPIError, TelegramChannelAdapter
from imcodex.models import InboundMessage, OutboundMessage


def _adapter(**kwargs) -> TelegramChannelAdapter:
    return TelegramChannelAdapter(
        enabled=True,
        bot_token="test-token",
        middleware=kwargs.pop("middleware", object()),
        access_policy=kwargs.pop("access_policy", ChannelAccessPolicy.allow_all()),
        **kwargs,
    )


def test_telegram_normalizes_private_message() -> None:
    adapter = _adapter()

    parsed = adapter.parse_inbound_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 7,
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": 42, "type": "private"},
                "text": "inspect repo",
            },
        }
    )

    assert parsed is not None
    inbound, reply_to = parsed
    assert inbound == InboundMessage(
        channel_id="telegram",
        conversation_id="chat:42",
        user_id="42",
        message_id="42:7",
        text="inspect repo",
    )
    assert reply_to == "7"


def test_telegram_requires_and_strips_group_mention() -> None:
    adapter = _adapter(require_mention=True)
    adapter._bot_username = "imcodex_bot"
    base = {
        "message_id": 9,
        "from": {"id": 42, "is_bot": False},
        "chat": {"id": -1001, "type": "supergroup"},
    }

    assert adapter.parse_inbound_update({"message": {**base, "text": "hello everyone"}}) is None
    assert adapter.parse_inbound_update({"message": {**base, "text": "/status@other_bot"}}) is None
    parsed = adapter.parse_inbound_update(
        {"message": {**base, "text": "@imcodex_bot inspect repo"}}
    )

    assert parsed is not None
    assert parsed[0].conversation_id == "chat:-1001"
    assert parsed[0].text == "inspect repo"


def test_telegram_keeps_forum_topics_as_distinct_conversations() -> None:
    adapter = _adapter(require_mention=True)

    parsed = adapter.parse_inbound_update(
        {
            "message": {
                "message_id": 9,
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "/status",
            }
        }
    )

    assert parsed is not None
    assert parsed[0].conversation_id == "chat:-1001:topic:77"


@pytest.mark.asyncio
async def test_telegram_sends_chunked_topic_reply() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        await adapter.send_message(
            OutboundMessage(
                channel_id="telegram",
                conversation_id="chat:-1001:topic:77",
                message_type="turn_result",
                text="a" * 4001,
                metadata={"reply_to_message_id": "9"},
            )
        )

    assert len(requests) == 2
    first = json.loads(requests[0].content)
    second = json.loads(requests[1].content)
    assert requests[0].url.path == "/bottest-token/sendMessage"
    assert len(first["text"]) == 4000
    assert first["chat_id"] == -1001
    assert first["message_thread_id"] == 77
    assert first["reply_parameters"] == {"message_id": 9}
    assert second["text"] == "a"
    assert "reply_parameters" not in second


@pytest.mark.asyncio
async def test_telegram_retries_rate_limit_with_server_delay() -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 2},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client, sleep=capture_sleep)
        await adapter.send_message(
            OutboundMessage(
                channel_id="telegram",
                conversation_id="chat:42",
                message_type="turn_result",
                text="done",
            )
        )

    assert attempts == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_telegram_poll_persists_offset_after_dispatch(tmp_path: Path) -> None:
    class Middleware:
        def __init__(self) -> None:
            self.messages: list[InboundMessage] = []

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None) -> None:
            self.messages.append(inbound)

    middleware = Middleware()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 15,
                        "message": {
                            "message_id": 7,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {"id": 42, "type": "private"},
                            "text": "hello",
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(middleware=middleware, http_client=client, state_dir=tmp_path)
        await adapter._poll_once()

    assert [message.text for message in middleware.messages] == ["hello"]
    assert json.loads((tmp_path / "polling-offset.json").read_text(encoding="utf-8")) == {
        "offset": 16
    }
    assert (tmp_path / "polling-offset.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_telegram_start_requires_a_token() -> None:
    adapter = TelegramChannelAdapter(enabled=True, bot_token="", middleware=object())

    with pytest.raises(RuntimeError, match="requires IMCODEX_TELEGRAM_BOT_TOKEN"):
        await adapter.start()


@pytest.mark.asyncio
async def test_telegram_api_error_does_not_include_bot_token() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await adapter._probe_bot()

    assert "test-token" not in str(exc_info.value)

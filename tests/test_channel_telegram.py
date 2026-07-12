from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import pytest

from imcodex.channels import (
    ChannelAccessPolicy,
    TelegramAPIError,
    TelegramChannelAdapter,
)
from imcodex.observability.logger import (
    configure_observability_logging,
    reset_observability_logging,
)
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
    parsed = adapter.parse_inbound_update({"message": {**base, "text": "@imcodex_bot inspect repo"}})

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
    state = json.loads((tmp_path / "polling-offset.json").read_text(encoding="utf-8"))
    assert state["offset"] == 16
    assert state["version"] == 1
    assert state["updated_at"] > 0
    if os.name != "nt":
        assert (tmp_path / "polling-offset.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_telegram_start_requires_a_token() -> None:
    adapter = TelegramChannelAdapter(enabled=True, bot_token="", middleware=object())

    with pytest.raises(RuntimeError, match="requires IMCODEX_TELEGRAM_BOT_TOKEN"):
        await adapter.start()


def test_telegram_startup_configuration_normalizes_api_base() -> None:
    adapter = _adapter(
        api_base="  https://api.telegram.org/  ",
        http_client=object(),
    )

    adapter.validate_startup_configuration()

    assert adapter.api_base == "https://api.telegram.org"


def test_telegram_startup_configuration_rejects_invalid_api_base() -> None:
    adapter = _adapter(
        api_base="https://user:password@api.telegram.org",
        http_client=object(),
    )

    with pytest.raises(ValueError, match="IMCODEX_TELEGRAM_API_BASE must not contain userinfo"):
        adapter.validate_startup_configuration()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission enforcement")
def test_telegram_token_file_must_be_private(tmp_path: Path) -> None:
    token_file = tmp_path / "bot-token"
    token_file.write_text("secret", encoding="utf-8")
    os.chmod(token_file, 0o644)
    adapter = TelegramChannelAdapter(
        enabled=True,
        bot_token="",
        bot_token_file=token_file,
        middleware=object(),
        http_client=object(),
    )

    with pytest.raises(RuntimeError, match="private file"):
        adapter._resolve_bot_token()

    os.chmod(token_file, 0o600)
    assert adapter._resolve_bot_token() == "secret"


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


@pytest.mark.asyncio
async def test_transport_logging_never_records_telegram_token(caplog, tmp_path: Path) -> None:
    token = "SUPERSECRET-telegram-token"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 7}})

    loggers = [logging.getLogger(name) for name in ("Lark", "httpx", "httpcore", "websockets")]
    old_levels = [item.level for item in loggers]
    try:
        for item in loggers:
            item.setLevel(logging.NOTSET)
        caplog.set_level(logging.DEBUG)
        log_path = tmp_path / "bridge.log"
        configure_observability_logging(
            level="DEBUG",
            instance_id="test-instance",
            log_paths=[log_path],
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TelegramChannelAdapter(
                enabled=True,
                bot_token=token,
                middleware=object(),
                access_policy=ChannelAccessPolicy.allow_all(),
                http_client=client,
            )
            await adapter._probe_bot()
        logging.getLogger("imcodex.test").debug("probe complete")
        for handler_item in logging.getLogger().handlers:
            handler_item.flush()

        assert token not in caplog.text
        assert token not in log_path.read_text(encoding="utf-8")
        assert all(item.getEffectiveLevel() >= logging.WARNING for item in loggers)
    finally:
        reset_observability_logging()
        for item, level in zip(loggers, old_levels, strict=True):
            item.setLevel(level)


@pytest.mark.asyncio
async def test_telegram_does_not_retry_ambiguous_send_failure() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        with pytest.raises(TelegramAPIError, match="network request failed"):
            await adapter.send_message(
                OutboundMessage(
                    channel_id="telegram",
                    conversation_id="chat:42",
                    message_type="turn_result",
                    text="done",
                )
            )

    assert attempts == 1


@pytest.mark.asyncio
async def test_telegram_polling_error_preserves_server_retry_after() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await adapter._poll_once()

    assert exc_info.value.retry_after == 7


@pytest.mark.asyncio
async def test_telegram_repeated_poll_failures_exponentially_back_off() -> None:
    delays: list[float] = []

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            adapter._stop_event.set()

    adapter = _adapter(sleep=capture_sleep)

    async def probe() -> None:
        adapter._bot_id = "7"

    async def fail_poll() -> None:
        raise TelegramAPIError(error_code=409, description="Conflict")

    adapter._probe_bot = probe  # type: ignore[method-assign]
    adapter._poll_once = fail_poll  # type: ignore[method-assign]

    await adapter._run_forever()

    assert delays == [1.0, 2.0, 4.0]
    await adapter.stop()


@pytest.mark.asyncio
async def test_telegram_outer_loop_honors_retry_after() -> None:
    delays: list[float] = []

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)
        adapter._stop_event.set()

    adapter = _adapter(sleep=capture_sleep)

    async def probe() -> None:
        adapter._bot_id = "7"

    async def fail_poll() -> None:
        raise TelegramAPIError(
            error_code=429,
            description="Too Many Requests",
            retry_after=9,
        )

    adapter._probe_bot = probe  # type: ignore[method-assign]
    adapter._poll_once = fail_poll  # type: ignore[method-assign]

    await adapter._run_forever()

    assert delays == [9]
    await adapter.stop()


@pytest.mark.asyncio
async def test_telegram_discards_offset_owned_by_another_bot(tmp_path: Path) -> None:
    (tmp_path / "polling-offset.json").write_text(
        json.dumps(
            {
                "version": 1,
                "bot_id": "old-bot",
                "offset": 9999,
                "updated_at": 90.0,
            }
        ),
        encoding="utf-8",
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client, state_dir=tmp_path, clock=lambda: 100.0)
        await adapter._probe_bot()

    state = json.loads((tmp_path / "polling-offset.json").read_text(encoding="utf-8"))
    assert adapter._offset is None
    assert state["bot_id"] == "7"
    assert state["offset"] is None


def test_telegram_fails_closed_on_corrupt_polling_offset(tmp_path: Path) -> None:
    (tmp_path / "polling-offset.json").write_text("{truncated", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid Telegram polling offset state"):
        _adapter(state_dir=tmp_path, http_client=object())

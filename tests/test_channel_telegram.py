from __future__ import annotations

import asyncio
from io import BytesIO
import json
import logging
import os
from pathlib import Path

import httpx
from PIL import Image
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
from imcodex.channels.media import IMAGE_DOWNLOAD_FAILED
from imcodex.models import InboundMessage, OutboundArtifact, OutboundMessage
from imcodex.store import ConversationStore


def _encoded_image(image_format: str = "PNG") -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(stream, format=image_format)
    return stream.getvalue()


PNG = _encoded_image()


class _PreparingMiddleware:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def handle_inbound(
        self,
        _adapter,
        inbound,
        *,
        reply_to_message_id=None,
        prepare_inbound=None,
        pending_attachment_count=0,
    ) -> None:
        if prepare_inbound is not None:
            inbound = await prepare_inbound(inbound)
        self.messages.append(inbound)


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


def test_telegram_accepts_image_only_and_selects_largest_photo_size() -> None:
    adapter = _adapter()

    parsed = adapter._parse_inbound_update(
        {
            "message": {
                "message_id": 10,
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": 42, "type": "private"},
                "photo": [
                    {"file_id": "medium", "width": 640, "height": 480, "file_size": 100},
                    {"file_id": "largest", "width": 1920, "height": 1080, "file_size": 200},
                    {"file_id": "small", "width": 160, "height": 120, "file_size": 50},
                ],
            }
        }
    )

    assert parsed is not None
    inbound, reply_to, references = parsed
    assert inbound.text == ""
    assert reply_to == "10"
    assert [reference.file_id for reference in references] == ["largest"]
    # Keep the existing public parsing contract: platform references stay private.
    assert adapter.parse_inbound_update(
        {
            "message": {
                "message_id": 10,
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": 42, "type": "private"},
                "photo": [{"file_id": "largest", "width": 1920, "height": 1080}],
            }
        }
    ) == (inbound, reply_to)


def test_telegram_accepts_only_image_like_documents() -> None:
    adapter = _adapter()
    base = {
        "message_id": 10,
        "from": {"id": 42, "is_bot": False},
        "chat": {"id": 42, "type": "private"},
    }

    mime_image = adapter._parse_inbound_update(
        {"message": {**base, "document": {"file_id": "png", "mime_type": "image/png"}}}
    )
    extension_image = adapter._parse_inbound_update(
        {
            "message": {
                **base,
                "document": {
                    "file_id": "webp",
                    "mime_type": "application/octet-stream",
                    "file_name": "IMAGE.WEBP",
                },
            }
        }
    )
    non_image = adapter._parse_inbound_update(
        {
            "message": {
                **base,
                "document": {
                    "file_id": "pdf",
                    "mime_type": "application/pdf",
                    "file_name": "report.pdf",
                },
            }
        }
    )

    assert mime_image is not None and mime_image[2][0].file_id == "png"
    assert extension_image is not None and extension_image[2][0].file_id == "webp"
    assert non_image is None


def test_telegram_preserves_malformed_image_envelopes_for_stable_error() -> None:
    adapter = _adapter()
    base = {
        "message_id": 10,
        "from": {"id": 42, "is_bot": False},
        "chat": {"id": 42, "type": "private"},
    }

    malformed_photo = adapter._parse_inbound_update(
        {"message": {**base, "photo": [{"width": 100, "height": 100}]}}
    )
    malformed_document = adapter._parse_inbound_update(
        {
            "message": {
                **base,
                "document": {"mime_type": "image/png", "file_name": "image.png"},
            }
        }
    )

    assert malformed_photo is not None and malformed_photo[2][0].file_id == ""
    assert malformed_document is not None and malformed_document[2][0].file_id == ""


@pytest.mark.asyncio
async def test_telegram_malformed_image_envelope_fails_without_media_api(
    tmp_path: Path,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError("malformed image must fail before getFile")

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            middleware=middleware,
            http_client=client,
            media_dir=tmp_path / "media",
        )
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "photo": [{"width": 100, "height": 100}],
                }
            }
        )

    assert requests == 0
    assert middleware.messages[0].input_error == IMAGE_DOWNLOAD_FAILED
    assert middleware.messages[0].attachments == ()


def test_telegram_group_images_follow_caption_mention_or_reply_targeting() -> None:
    adapter = _adapter(require_mention=True)
    adapter._bot_id = "7"
    adapter._bot_username = "imcodex_bot"
    base = {
        "message_id": 9,
        "from": {"id": 42, "is_bot": False},
        "chat": {"id": -1001, "type": "supergroup"},
        "photo": [{"file_id": "photo", "width": 100, "height": 100}],
    }

    assert adapter.parse_inbound_update({"message": base}) is None
    mentioned = adapter.parse_inbound_update(
        {"message": {**base, "caption": "@imcodex_bot"}}
    )
    replied = adapter.parse_inbound_update(
        {
            "message": {
                **base,
                "reply_to_message": {"from": {"id": 7, "is_bot": True}},
            }
        }
    )

    assert mentioned is not None and mentioned[0].text == ""
    assert replied is not None and replied[0].text == ""


@pytest.mark.asyncio
async def test_telegram_downloads_file_from_getfile_into_shared_image_contract(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/proxy/bottest-token/getFile":
            assert json.loads(request.content) == {"file_id": "largest"}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_id": "largest",
                        "file_path": "photos/file 1.png",
                    },
                },
            )
        if request.url.path == "/proxy/file/bottest-token/photos/file 1.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "application/pdf"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            middleware=middleware,
            http_client=client,
            api_base="https://telegram.example/proxy",
            media_dir=tmp_path / "media",
        )
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "caption": "describe this",
                    "photo": [{"file_id": "largest", "width": 1920, "height": 1080}],
                }
            }
        )

    assert [request.method for request in requests] == ["POST", "GET"]
    assert all(request.url.host == "telegram.example" for request in requests)
    assert requests[1].headers["Accept-Encoding"] == "identity"
    assert len(middleware.messages) == 1
    inbound = middleware.messages[0]
    assert inbound.text == "describe this"
    assert inbound.input_error is None
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].content_type == "image/png"
    assert inbound.attachments[0].size_bytes == len(PNG)
    assert Path(inbound.attachments[0].local_path).is_absolute()
    assert Path(inbound.attachments[0].local_path).read_bytes() == PNG


@pytest.mark.parametrize(
    "file_path",
    [
        "/tmp/image.png",
        r"C:\\tmp\\image.png",
        "../image.png",
        "%2e%2e/image.png",
        "https://attacker.invalid/image.png",
        "photos\\image.png",
    ],
)
@pytest.mark.asyncio
async def test_telegram_rejects_non_relative_getfile_paths_before_download(
    tmp_path: Path,
    file_path: str,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"file_path": file_path}},
        )

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(middleware=middleware, http_client=client, media_dir=tmp_path)
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "photo": [{"file_id": "image", "width": 100, "height": 100}],
                }
            }
        )

    assert [request.method for request in requests] == ["POST"]
    assert middleware.messages[0].input_error == IMAGE_DOWNLOAD_FAILED
    assert middleware.messages[0].attachments == ()


@pytest.mark.asyncio
async def test_telegram_media_download_does_not_follow_redirects_with_bot_token(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/image.png"}},
            )
        return httpx.Response(302, headers={"location": "https://attacker.invalid/stolen"})

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(middleware=middleware, http_client=client, media_dir=tmp_path)
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "photo": [{"file_id": "image", "width": 100, "height": 100}],
                }
            }
        )

    assert len(requests) == 2
    assert all(request.url.host == "api.telegram.org" for request in requests)
    assert middleware.messages[0].input_error == IMAGE_DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_telegram_rejects_http_content_encoding_before_decoding(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/image.png"}},
            )
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            content=PNG,
            headers={"Content-Encoding": "gzip"},
        )

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(middleware=middleware, http_client=client, media_dir=tmp_path)
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "photo": [{"file_id": "image", "width": 100, "height": 100}],
                }
            }
        )

    assert middleware.messages[0].input_error == IMAGE_DOWNLOAD_FAILED
    assert middleware.messages[0].attachments == ()


@pytest.mark.asyncio
async def test_telegram_access_policy_runs_before_getfile(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked image must not access Telegram media APIs")

    middleware = _PreparingMiddleware()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            middleware=middleware,
            http_client=client,
            media_dir=tmp_path,
            access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"owner"})),
        )
        await adapter.handle_update(
            {
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "photo": [{"file_id": "image", "width": 100, "height": 100}],
                }
            }
        )

    assert middleware.messages == []


@pytest.mark.asyncio
async def test_telegram_stable_update_is_deduplicated_before_second_download(
    tmp_path: Path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    class Service:
        def __init__(self) -> None:
            self.store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
            self.messages: list[InboundMessage] = []

        async def handle_inbound(self, inbound: InboundMessage) -> list[OutboundMessage]:
            self.messages.append(inbound)
            return []

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/image.png"}},
            )
        return httpx.Response(200, content=PNG)

    service = Service()
    update = {
        "message": {
            "message_id": 10,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 42, "type": "private"},
            "caption": "describe this",
            "photo": [{"file_id": "image", "width": 100, "height": 100}],
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            middleware=UnifiedChannelMiddleware(service=service),
            http_client=client,
            media_dir=tmp_path / "media",
        )
        await adapter.handle_update(update)
        await adapter.handle_update(update)

    assert [request.method for request in requests] == ["POST", "GET"]
    assert len(service.messages) == 1
    assert len(service.messages[0].attachments) == 1


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


@pytest.mark.asyncio
async def test_telegram_starts_and_stops_media_materializer() -> None:
    class Materializer:
        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0

        async def start(self) -> None:
            self.started += 1

        async def stop(self) -> None:
            self.stopped += 1

    materializer = Materializer()
    runner_started = asyncio.Event()
    runner_blocked = asyncio.Event()
    adapter = _adapter(
        http_client=object(),
        media_materializer=materializer,  # type: ignore[arg-type]
    )

    async def idle_runner() -> None:
        runner_started.set()
        await runner_blocked.wait()

    adapter._run_forever = idle_runner  # type: ignore[method-assign]
    await adapter.start()
    await asyncio.wait_for(runner_started.wait(), timeout=1)
    await adapter.stop()

    assert materializer.started == 1
    assert materializer.stopped == 1


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
async def test_telegram_sends_staged_image_before_terminal_text(tmp_path: Path) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    image_path = outbound_root / "preview.png"
    image_path.write_bytes(PNG)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            http_client=client,
            outbound_media_dir=outbound_root,
        )
        message = OutboundMessage(
            channel_id="telegram",
            conversation_id="chat:-1001:topic:77",
            message_type="turn_result",
            text="Rendered preview.",
            metadata={"reply_to_message_id": "42"},
            artifacts=[
                OutboundArtifact(
                    kind="image",
                    local_path=str(image_path),
                    content_type="image/png",
                    filename="preview.png",
                    size_bytes=len(PNG),
                )
            ],
        )
        await adapter.send_message(message)

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "sendPhoto",
        "sendMessage",
    ]
    upload = requests[0].content
    assert b'name="photo"; filename="preview.png"' in upload
    assert PNG in upload
    assert b'name="message_thread_id"' in upload and b"77" in upload
    assert b'"message_id": 42' in upload
    assert message.artifacts == []


@pytest.mark.asyncio
async def test_telegram_preserves_artifact_across_permission_recovery(tmp_path: Path) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    image_path = outbound_root / "preview.png"
    image_path.write_bytes(PNG)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                403,
                json={"ok": False, "error_code": 403, "description": "Forbidden"},
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    artifact = OutboundArtifact(
        kind="image",
        local_path=str(image_path),
        content_type="image/png",
        filename="preview.png",
        size_bytes=len(PNG),
    )
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat:123",
        message_type="turn_result",
        text="Rendered preview.",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[artifact],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client, outbound_media_dir=outbound_root)
        with pytest.raises(TelegramAPIError, match="403"):
            await adapter.send_message(message)
        assert message.artifacts == [artifact]

        await adapter.send_message(message)

    assert message.artifacts == []
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "sendPhoto",
        "sendPhoto",
        "sendMessage",
    ]


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

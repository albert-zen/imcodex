from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image
import pytest

from imcodex.appserver.thread_dynamic_tools import (
    native_thread_dynamic_tool_specs,
)
from imcodex.channels import (
    ChannelAccessPolicy,
    FeishuChannelAdapter,
    QQChannelAdapter,
    TelegramChannelAdapter,
    WebhookOutboundSink,
    WeixinChannelAdapter,
    create_app,
)
from imcodex.channels.media import (
    FileMediaResult,
    MaterializedFile,
    MaterializedImage,
    MediaResult,
)
from imcodex.channels.weixin_state import WeixinTransportState

from .system_harness import (
    NativeStep,
    ScriptedNativeProcess,
    SystemHarness,
    build_system_harness,
    queue_new_thread_turn,
    wait_until,
)


SYSTEM_CWD = "/workspace/imcodex"
MODEL_ANSWER = "Mocked model answer from native Codex"


@dataclass(slots=True)
class ChannelDriver:
    channel_id: str
    conversation_id: str
    send_text: Callable[[str, str], Awaitable[None]]
    send_image: Callable[[str, str], Awaitable[None]]
    send_file: Callable[[str, str], Awaitable[None]]
    outbound_texts: Callable[[], list[str]]
    platform_requests: Callable[[], list[dict]]
    image_references: Callable[[], tuple[Any, ...]]
    file_references: Callable[[], tuple[Any, ...]]
    inbound_image_path: Path
    inbound_file_path: Path
    send_quote: Callable[[str, str], Awaitable[None]] | None = None


class _FakeFeishuSdk:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, dict]] = []

    async def send(self, to: str, message: dict, opts: dict):
        self.sent.append((to, message, opts))
        return SimpleNamespace(success=True, message_id=f"out-{len(self.sent)}")


class _FakeWeixinTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, **payload) -> str:
        self.sent.append(payload)
        return f"out-{len(self.sent)}"


class _StaticImageMaterializer:
    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        self.calls = 0
        self.references: tuple[Any, ...] = ()

    async def materialize(self, references) -> MediaResult:
        self.calls += 1
        self.references = tuple(references)
        return MediaResult(
            images=(
                MaterializedImage(
                    content_type="image/png",
                    local_path=str(self.image_path),
                    size_bytes=self.image_path.stat().st_size,
                ),
            )
        )


class _StaticFileMaterializer:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.calls = 0
        self.references: tuple[Any, ...] = ()

    async def materialize(self, references) -> FileMediaResult:
        self.calls += 1
        self.references = tuple(references)
        return FileMediaResult(
            files=(
                MaterializedFile(
                    content_type="text/markdown",
                    local_path=str(self.file_path),
                    size_bytes=self.file_path.stat().st_size,
                    filename="requirements.md",
                ),
            )
        )


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(stream, format="PNG")
    return stream.getvalue()


def _valid_dynamic_image_input(native_input: list[dict]) -> bool:
    if (
        len(native_input) != 2
        or native_input[0] != {"type": "text", "text": "describe the image"}
        or native_input[1].get("type") != "localImage"
    ):
        return False
    path = Path(str(native_input[1].get("path") or ""))
    return path.is_file() and path.suffix == ".png"


def _valid_dynamic_file_input(native_input: list[dict]) -> bool:
    if len(native_input) != 1 or native_input[0].get("type") != "text":
        return False
    manifest = str(native_input[0].get("text") or "")
    prefix = (
        "review the requirements\n\n"
        "[Attachment]\n"
        "- requirements.md\n"
        "  Path: "
    )
    if not manifest.startswith(prefix):
        return False
    path = Path(manifest.removeprefix(prefix))
    return path.is_file() and path.read_text(encoding="utf-8") == "# Requirements\n"


async def _wait_for_native_turn_settled(
    harness: SystemHarness,
    thread_id: str,
) -> None:
    await wait_until(
        lambda: (
            harness.store.get_active_turn(thread_id) is None
            and harness.store.list_terminal_delivery_watches(thread_id) == []
            and harness.store.list_pending_terminal_deliveries(thread_id) == []
        )
    )


async def _install_channel(
    harness: SystemHarness,
    channel_id: str,
    tmp_path: Path,
) -> ChannelDriver:
    image_path = tmp_path / f"{channel_id}-inbound.png"
    image_path.write_bytes(_png_bytes())
    image_materializer = _StaticImageMaterializer(image_path)
    file_path = tmp_path / f"{channel_id}-requirements.md"
    file_path.write_text("# Requirements\n", encoding="utf-8")
    file_materializer = _StaticFileMaterializer(file_path)

    if channel_id == "qq":
        sent: list[dict] = []

        async def qq_http(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/app/getAppAccessToken":
                return httpx.Response(
                    200,
                    json={"access_token": "test-token", "expires_in": 7200},
                )
            sent.append(
                {
                    "path": request.url.path,
                    "authorization": request.headers.get("Authorization"),
                    "body": json.loads(request.content),
                }
            )
            return httpx.Response(200, json={"id": f"out-{len(sent)}"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(qq_http))
        harness.closeables.append(client)
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="test-app",
            client_secret="test-secret",
            middleware=harness.middleware,
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=False,
            access_policy=ChannelAccessPolicy.allow_all(),
            media_materializer=image_materializer,
            file_materializer=file_materializer,
        )
        harness.outbound.channel_sinks[channel_id] = adapter

        async def send_text(message_id: str, text: str) -> None:
            await adapter.handle_dispatch_event(
                "C2C_MESSAGE_CREATE",
                {
                    "id": message_id,
                    "content": text,
                    "author": {"user_openid": "qq-owner"},
                },
            )

        async def send_image(message_id: str, text: str) -> None:
            await adapter.handle_dispatch_event(
                "C2C_MESSAGE_CREATE",
                {
                    "id": message_id,
                    "content": text,
                    "author": {"user_openid": "qq-owner"},
                    "attachments": [
                        {
                            "content_type": "image/png",
                            "filename": "screen.png",
                            "url": "https://example.qpic.cn/private/screen.png",
                        }
                    ],
                },
            )

        async def send_file(message_id: str, text: str) -> None:
            await adapter.handle_dispatch_event(
                "C2C_MESSAGE_CREATE",
                {
                    "id": message_id,
                    "content": text,
                    "author": {"user_openid": "qq-owner"},
                    "attachments": [
                        {
                            "content_type": "text/markdown",
                            "filename": "requirements.md",
                            "url": "https://example.qpic.cn/private/requirements.md",
                        }
                    ],
                },
            )

        async def send_quote(message_id: str, text: str) -> None:
            await adapter.handle_dispatch_event(
                "C2C_MESSAGE_CREATE",
                {
                    "id": message_id,
                    "content": text,
                    "author": {"user_openid": "qq-owner"},
                    "message_type": 103,
                    "message_scene": {"ext": ["ref_msg_idx=stale-reference"]},
                    "msg_elements": [
                        {
                            "msg_idx": "quoted-reference",
                            "content": "Ship plan A first",
                            "attachments": [
                                {
                                    "content_type": "image/png",
                                    "filename": "plan.png",
                                    "url": "https://signed.example.invalid/private",
                                }
                            ],
                        }
                    ],
                },
            )

        return ChannelDriver(
            channel_id=channel_id,
            conversation_id="c2c:qq-owner",
            send_text=send_text,
            send_image=send_image,
            send_file=send_file,
            outbound_texts=lambda: [
                str(payload["body"].get("content") or "") for payload in sent
            ],
            platform_requests=lambda: list(sent),
            image_references=lambda: image_materializer.references,
            file_references=lambda: file_materializer.references,
            inbound_image_path=image_path,
            inbound_file_path=file_path,
            send_quote=send_quote,
        )

    if channel_id == "telegram":
        sent: list[dict] = []

        async def telegram_http(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("/sendMessage"):
                sent.append({"path": request.url.path, "body": body})
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": len(sent) + 100}},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(telegram_http))
        harness.closeables.append(client)
        adapter = TelegramChannelAdapter(
            enabled=True,
            bot_token="test-token",
            middleware=harness.middleware,
            http_client=client,
            access_policy=ChannelAccessPolicy.allow_all(),
            media_materializer=image_materializer,
            file_materializer=file_materializer,
        )
        harness.outbound.channel_sinks[channel_id] = adapter

        async def send_text(message_id: str, text: str) -> None:
            await adapter.handle_update(
                {
                    "update_id": int(message_id),
                    "message": {
                        "message_id": int(message_id),
                        "from": {"id": 42, "is_bot": False},
                        "chat": {"id": 42, "type": "private"},
                        "text": text,
                    },
                }
            )

        async def send_image(message_id: str, text: str) -> None:
            await adapter.handle_update(
                {
                    "update_id": int(message_id),
                    "message": {
                        "message_id": int(message_id),
                        "from": {"id": 42, "is_bot": False},
                        "chat": {"id": 42, "type": "private"},
                        "caption": text,
                        "photo": [
                            {
                                "file_id": "private-file-id",
                                "width": 800,
                                "height": 600,
                            }
                        ],
                    },
                }
            )

        async def send_file(message_id: str, text: str) -> None:
            await adapter.handle_update(
                {
                    "update_id": int(message_id),
                    "message": {
                        "message_id": int(message_id),
                        "from": {"id": 42, "is_bot": False},
                        "chat": {"id": 42, "type": "private"},
                        "caption": text,
                        "document": {
                            "file_id": "private-document-id",
                            "file_name": "requirements.md",
                            "mime_type": "text/markdown",
                        },
                    },
                }
            )

        return ChannelDriver(
            channel_id=channel_id,
            conversation_id="chat:42",
            send_text=send_text,
            send_image=send_image,
            send_file=send_file,
            outbound_texts=lambda: [
                str(payload["body"].get("text") or "") for payload in sent
            ],
            platform_requests=lambda: list(sent),
            image_references=lambda: image_materializer.references,
            file_references=lambda: file_materializer.references,
            inbound_image_path=image_path,
            inbound_file_path=file_path,
        )

    if channel_id == "feishu":
        sdk = _FakeFeishuSdk()
        adapter = FeishuChannelAdapter(
            enabled=True,
            app_id="test-app",
            app_secret="test-secret",
            middleware=harness.middleware,
            channel_factory=lambda **_config: sdk,
            access_policy=ChannelAccessPolicy.allow_all(),
            media_materializer=image_materializer,
            file_materializer=file_materializer,
        )
        adapter._sdk = sdk
        harness.outbound.channel_sinks[channel_id] = adapter

        async def send_text(message_id: str, text: str) -> None:
            await adapter.handle_sdk_message(
                SimpleNamespace(
                    id=message_id,
                    message_id=message_id,
                    raw_content_type="text",
                    content_text=text,
                    resources=[],
                    mentioned_bot=False,
                    conversation=SimpleNamespace(
                        chat_id="feishu-chat",
                        chat_type="p2p",
                        thread_id=None,
                    ),
                    sender=SimpleNamespace(open_id="feishu-owner"),
                )
            )

        async def send_image(message_id: str, text: str) -> None:
            await adapter.handle_sdk_message(
                SimpleNamespace(
                    id=message_id,
                    message_id=message_id,
                    raw_content_type="image",
                    content_text=f"{text}\n![image](private-image-key)",
                    resources=[
                        SimpleNamespace(
                            type="image",
                            file_key="private-image-key",
                        )
                    ],
                    mentioned_bot=False,
                    conversation=SimpleNamespace(
                        chat_id="feishu-chat",
                        chat_type="p2p",
                        thread_id=None,
                    ),
                    sender=SimpleNamespace(open_id="feishu-owner"),
                )
            )

        async def send_file(message_id: str, text: str) -> None:
            await adapter.handle_sdk_message(
                SimpleNamespace(
                    id=message_id,
                    message_id=message_id,
                    raw_content_type="file",
                    content_text=text,
                    resources=[
                        SimpleNamespace(
                            type="file",
                            file_key="private-file-key",
                            file_name="requirements.md",
                            content_type="text/markdown",
                        )
                    ],
                    mentioned_bot=False,
                    conversation=SimpleNamespace(
                        chat_id="feishu-chat",
                        chat_type="p2p",
                        thread_id=None,
                    ),
                    sender=SimpleNamespace(open_id="feishu-owner"),
                )
            )

        return ChannelDriver(
            channel_id=channel_id,
            conversation_id="chat:feishu-chat",
            send_text=send_text,
            send_image=send_image,
            send_file=send_file,
            outbound_texts=lambda: [
                str(message.get("text") or "")
                for _to, message, _opts in sdk.sent
            ],
            platform_requests=lambda: [
                {"to": to, "message": message, "opts": opts}
                for to, message, opts in sdk.sent
            ],
            image_references=lambda: image_materializer.references,
            file_references=lambda: file_materializer.references,
            inbound_image_path=image_path,
            inbound_file_path=file_path,
        )

    if channel_id == "weixin":
        transport = _FakeWeixinTransport()
        adapter = WeixinChannelAdapter(
            enabled=True,
            middleware=harness.middleware,
            state_dir=tmp_path / "weixin-state",
            access_policy=ChannelAccessPolicy.allow_all(),
            media_materializer=image_materializer,
            file_materializer=file_materializer,
        )
        adapter._transport = transport
        adapter._state = WeixinTransportState()
        harness.outbound.channel_sinks[channel_id] = adapter

        async def send_text(message_id: str, text: str) -> None:
            await adapter.handle_raw_message(
                {
                    "message_id": int(message_id),
                    "from_user_id": "owner@im.wechat",
                    "message_type": 1,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                    "context_token": "test-context-token",
                }
            )

        async def send_image(message_id: str, text: str) -> None:
            await adapter.handle_raw_message(
                {
                    "message_id": int(message_id),
                    "from_user_id": "owner@im.wechat",
                    "message_type": 1,
                    "message_state": 2,
                    "item_list": [
                        {"type": 1, "text_item": {"text": text}},
                        {
                            "type": 2,
                            "image_item": {
                                "media": {
                                    "encrypt_query_param": "private-image-ticket",
                                    "aes_key": "AAECAwQFBgcICQoLDA0ODw==",
                                }
                            },
                        },
                    ],
                    "context_token": "test-context-token",
                }
            )

        async def send_file(message_id: str, text: str) -> None:
            await adapter.handle_raw_message(
                {
                    "message_id": int(message_id),
                    "from_user_id": "owner@im.wechat",
                    "message_type": 1,
                    "message_state": 2,
                    "item_list": [
                        {"type": 1, "text_item": {"text": text}},
                        {
                            "type": 4,
                            "file_item": {
                                "file_name": "requirements.md",
                                "content_type": "text/markdown",
                                "media": {
                                    "encrypt_query_param": "private-file-ticket",
                                    "aes_key": "AAECAwQFBgcICQoLDA0ODw==",
                                },
                            },
                        },
                    ],
                    "context_token": "test-context-token",
                }
            )

        return ChannelDriver(
            channel_id=channel_id,
            conversation_id="user:owner@im.wechat",
            send_text=send_text,
            send_image=send_image,
            send_file=send_file,
            outbound_texts=lambda: [
                str(payload.get("text") or "") for payload in transport.sent
            ],
            platform_requests=lambda: list(transport.sent),
            image_references=lambda: image_materializer.references,
            file_references=lambda: file_materializer.references,
            inbound_image_path=image_path,
            inbound_file_path=file_path,
        )

    if channel_id == "webhook":
        sent: list[dict] = []

        async def outbound_http(request: httpx.Request) -> httpx.Response:
            sent.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("Authorization"),
                    "body": json.loads(request.content),
                }
            )
            return httpx.Response(200, json={"ok": True})

        outbound_client = httpx.AsyncClient(
            transport=httpx.MockTransport(outbound_http)
        )
        harness.closeables.append(outbound_client)
        harness.outbound.default_sink = WebhookOutboundSink(
            "https://gateway.example.test/outbound",
            client=outbound_client,
            bearer_token="outbound-secret",
        )
        app = create_app(
            harness.service,
            inbound_token="inbound-secret",
            media_dir=tmp_path / "webhook-media",
        )
        inbound_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://imcodex.test",
        )
        harness.closeables.append(inbound_client)

        async def send_text(message_id: str, text: str) -> None:
            response = await inbound_client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer inbound-secret"},
                json={
                    "channel_id": "test-gateway",
                    "conversation_id": "gateway-conversation",
                    "user_id": "gateway-owner",
                    "message_id": message_id,
                    "text": text,
                },
            )
            assert response.status_code == 200

        async def send_image(message_id: str, text: str) -> None:
            response = await inbound_client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer inbound-secret"},
                data={
                    "channel_id": "test-gateway",
                    "conversation_id": "gateway-conversation",
                    "user_id": "gateway-owner",
                    "message_id": message_id,
                    "text": text,
                },
                files={
                    "images": ("screen.png", _png_bytes(), "image/png"),
                },
            )
            assert response.status_code == 200

        async def send_file(message_id: str, text: str) -> None:
            response = await inbound_client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer inbound-secret"},
                data={
                    "channel_id": "test-gateway",
                    "conversation_id": "gateway-conversation",
                    "user_id": "gateway-owner",
                    "message_id": message_id,
                    "text": text,
                },
                files={
                    "files": (
                        "requirements.md",
                        b"# Requirements\n",
                        "text/markdown",
                    ),
                },
            )
            assert response.status_code == 200

        return ChannelDriver(
            channel_id="test-gateway",
            conversation_id="gateway-conversation",
            send_text=send_text,
            send_image=send_image,
            send_file=send_file,
            outbound_texts=lambda: [
                str(payload["body"].get("text") or "") for payload in sent
            ],
            platform_requests=lambda: list(sent),
            image_references=lambda: (),
            file_references=lambda: (),
            inbound_image_path=image_path,
            inbound_file_path=file_path,
        )

    raise AssertionError(f"Unsupported test channel: {channel_id}")


def _assert_platform_delivery(
    platform_channel: str,
    requests: list[dict],
    expected_text: str,
) -> None:
    assert len(requests) == 1
    request = requests[0]
    if platform_channel == "qq":
        assert request == {
            "path": "/v2/users/qq-owner/messages",
            "authorization": "QQBot test-token",
            "body": {
                "content": expected_text,
                "msg_type": 0,
                "msg_seq": request["body"]["msg_seq"],
                "msg_id": "101",
            },
        }
        assert isinstance(request["body"]["msg_seq"], int)
        assert request["body"]["msg_seq"] > 0
        return
    if platform_channel == "telegram":
        assert request == {
            "path": "/bottest-token/sendMessage",
            "body": {
                "chat_id": 42,
                "text": expected_text,
                "link_preview_options": {"is_disabled": True},
            },
        }
        return
    if platform_channel == "feishu":
        assert request["to"] == "feishu-chat"
        assert request["message"] == {"text": expected_text}
        assert request["opts"]["receive_id_type"] == "chat_id"
        assert request["opts"]["reply_to"] == "101"
        assert len(request["opts"]["uuid"]) == 50
        return
    if platform_channel == "weixin":
        assert request["to_user_id"] == "owner@im.wechat"
        assert request["text"] == expected_text
        assert request["context_token"] == "test-context-token"
        assert request["client_id"].endswith(":0")
        return
    assert platform_channel == "webhook"
    assert request["url"] == "https://gateway.example.test/outbound"
    assert request["authorization"] == "Bearer outbound-secret"
    assert request["body"]["channel_id"] == "test-gateway"
    assert request["body"]["conversation_id"] == "gateway-conversation"
    assert request["body"]["message_type"] == "agentMessage"
    assert request["body"]["text"] == expected_text
    assert request["body"]["metadata"]["phase"] == "final_answer"


def _assert_image_reference_normalization(
    platform_channel: str,
    references: tuple[Any, ...],
) -> None:
    assert len(references) == 1
    reference = references[0]
    if platform_channel == "qq":
        assert reference.url == "https://example.qpic.cn/private/screen.png"
    elif platform_channel == "telegram":
        assert reference.file_id == "private-file-id"
    elif platform_channel == "feishu":
        assert reference.message_id == "301"
        assert reference.file_key == "private-image-key"
    else:
        assert platform_channel == "weixin"
        assert reference.encrypted_query_param == "private-image-ticket"
        assert reference.aes_key == "AAECAwQFBgcICQoLDA0ODw=="


def _assert_file_reference_normalization(
    platform_channel: str,
    references: tuple[Any, ...],
) -> None:
    assert len(references) == 1
    reference = references[0]
    assert reference.filename == "requirements.md"
    assert reference.content_type == "text/markdown"
    if platform_channel == "qq":
        assert reference.url == (
            "https://example.qpic.cn/private/requirements.md"
        )
    elif platform_channel == "telegram":
        assert reference.file_id == "private-document-id"
    elif platform_channel == "feishu":
        assert reference.message_id == "401"
        assert reference.file_key == "private-file-key"
    else:
        assert platform_channel == "weixin"
        assert reference.encrypted_query_param == "private-file-ticket"
        assert reference.aes_key == "AAECAwQFBgcICQoLDA0ODw=="


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform_channel",
    ["qq", "telegram", "feishu", "weixin", "webhook"],
)
async def test_real_channel_ingress_reaches_native_and_projects_back_to_platform(
    platform_channel: str,
    tmp_path: Path,
) -> None:
    process = ScriptedNativeProcess()
    process.queue("initialize", NativeStep(result={"ok": True}))
    harness = build_system_harness(tmp_path, process)
    driver = await _install_channel(harness, platform_channel, tmp_path)
    harness.store.set_bootstrap_cwd(
        driver.channel_id,
        driver.conversation_id,
        SYSTEM_CWD,
    )
    queue_new_thread_turn(
        process,
        thread_id=f"thread-{platform_channel}",
        turn_id=f"turn-{platform_channel}",
        cwd=SYSTEM_CWD,
        answer=MODEL_ANSWER,
        expected_input=[
            {"type": "text", "text": "inspect the end-to-end path"}
        ],
    )

    try:
        await driver.send_text("101", "inspect the end-to-end path")
        await wait_until(lambda: MODEL_ANSWER in driver.outbound_texts())
        await _wait_for_native_turn_settled(
            harness,
            f"thread-{platform_channel}",
        )

        turn_start = process.requests("turn/start")
        assert len(turn_start) == 1
        initialize = process.requests("initialize")
        assert len(initialize) == 1
        assert initialize[0]["params"]["capabilities"]["experimentalApi"] is True
        assert turn_start[0]["params"]["input"] == [
            {"type": "text", "text": "inspect the end-to-end path"}
        ]
        assert MODEL_ANSWER in driver.outbound_texts()
        _assert_platform_delivery(
            platform_channel,
            driver.platform_requests(),
            MODEL_ANSWER,
        )
        process.assert_consumed()
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform_channel",
    ["qq", "telegram", "feishu", "weixin", "webhook"],
)
async def test_real_channel_image_ingress_becomes_native_local_image(
    platform_channel: str,
    tmp_path: Path,
) -> None:
    process = ScriptedNativeProcess()
    process.queue("initialize", NativeStep(result={"ok": True}))
    harness = build_system_harness(tmp_path, process)
    driver = await _install_channel(harness, platform_channel, tmp_path)
    harness.store.set_bootstrap_cwd(
        driver.channel_id,
        driver.conversation_id,
        SYSTEM_CWD,
    )
    image_step = {
        "input_validator": _valid_dynamic_image_input
    } if platform_channel == "webhook" else {
        "expected_input": [
            {"type": "text", "text": "describe the image"},
            {
                "type": "localImage",
                "path": str(driver.inbound_image_path),
            },
        ]
    }
    queue_new_thread_turn(
        process,
        thread_id=f"image-thread-{platform_channel}",
        turn_id=f"image-turn-{platform_channel}",
        cwd=SYSTEM_CWD,
        answer=f"Image accepted from {platform_channel}",
        **image_step,
    )

    try:
        await driver.send_image("301", "describe the image")
        await wait_until(
            lambda: f"Image accepted from {platform_channel}"
            in driver.outbound_texts()
        )
        await _wait_for_native_turn_settled(
            harness,
            f"image-thread-{platform_channel}",
        )

        native_input = process.requests("turn/start")[0]["params"]["input"]
        assert native_input[0] == {"type": "text", "text": "describe the image"}
        assert native_input[1]["type"] == "localImage"
        image_path = Path(native_input[1]["path"])
        assert image_path.is_file()
        assert image_path.suffix == ".png"
        if platform_channel != "webhook":
            _assert_image_reference_normalization(
                platform_channel,
                driver.image_references(),
            )
        process.assert_consumed()
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform_channel",
    ["qq", "telegram", "feishu", "weixin", "webhook"],
)
async def test_real_channel_file_ingress_becomes_native_readable_manifest(
    platform_channel: str,
    tmp_path: Path,
) -> None:
    process = ScriptedNativeProcess()
    process.queue("initialize", NativeStep(result={"ok": True}))
    harness = build_system_harness(tmp_path, process)
    driver = await _install_channel(harness, platform_channel, tmp_path)
    harness.store.set_bootstrap_cwd(
        driver.channel_id,
        driver.conversation_id,
        SYSTEM_CWD,
    )
    file_step = {
        "input_validator": _valid_dynamic_file_input
    } if platform_channel == "webhook" else {
        "expected_input": [
            {
                "type": "text",
                "text": (
                    "review the requirements\n\n"
                    "[Attachment]\n"
                    "- requirements.md\n"
                    f"  Path: {driver.inbound_file_path}"
                ),
            }
        ]
    }
    queue_new_thread_turn(
        process,
        thread_id=f"file-thread-{platform_channel}",
        turn_id=f"file-turn-{platform_channel}",
        cwd=SYSTEM_CWD,
        answer=f"File accepted from {platform_channel}",
        **file_step,
    )

    try:
        await driver.send_file("401", "review the requirements")
        await wait_until(
            lambda: f"File accepted from {platform_channel}"
            in driver.outbound_texts()
        )
        await _wait_for_native_turn_settled(
            harness,
            f"file-thread-{platform_channel}",
        )

        native_input = process.requests("turn/start")[0]["params"]["input"]
        assert len(native_input) == 1
        assert native_input[0]["type"] == "text"
        manifest = native_input[0]["text"]
        assert manifest.startswith("review the requirements\n\n[Attachment]\n")
        assert "- requirements.md" in manifest
        staged_path = manifest.rsplit("  Path: ", 1)[-1]
        assert Path(staged_path).read_text(encoding="utf-8") == "# Requirements\n"
        if platform_channel != "webhook":
            _assert_file_reference_normalization(
                platform_channel,
                driver.file_references(),
            )
        process.assert_consumed()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_real_qq_quote_ingress_preserves_bounded_context_in_native_input(
    tmp_path: Path,
) -> None:
    process = ScriptedNativeProcess()
    process.queue("initialize", NativeStep(result={"ok": True}))
    harness = build_system_harness(tmp_path, process)
    driver = await _install_channel(harness, "qq", tmp_path)
    harness.store.set_bootstrap_cwd(
        driver.channel_id,
        driver.conversation_id,
        SYSTEM_CWD,
    )
    queue_new_thread_turn(
        process,
        thread_id="quote-thread",
        turn_id="quote-turn",
        cwd=SYSTEM_CWD,
        answer="Quote context accepted",
        expected_input=[
            {
                "type": "text",
                "text": (
                    "[Quoted message begins]\n"
                    "> Ship plan A first\n"
                    "> [image: plan.png]\n"
                    "[Quoted message ends]\n"
                    "[Current message]\n"
                    "What about this conclusion?"
                ),
            }
        ],
    )

    try:
        assert driver.send_quote is not None
        await driver.send_quote("501", "What about this conclusion?")
        await wait_until(lambda: "Quote context accepted" in driver.outbound_texts())
        await _wait_for_native_turn_settled(harness, "quote-thread")

        native_text = process.requests("turn/start")[0]["params"]["input"][0]["text"]
        assert native_text == (
            "[Quoted message begins]\n"
            "> Ship plan A first\n"
            "> [image: plan.png]\n"
            "[Quoted message ends]\n"
            "[Current message]\n"
            "What about this conclusion?"
        )
        assert "signed.example.invalid" not in native_text
        process.assert_consumed()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_thread_panel_pick_continuation_credits_and_new_thread_share_one_native_flow(
    tmp_path: Path,
) -> None:
    process = ScriptedNativeProcess()
    process.queue("initialize", NativeStep(result={"ok": True}))
    process.queue(
        "thread/list",
        NativeStep(
            result={
                "data": [
                    {
                        "id": "thread-selected",
                        "cwd": SYSTEM_CWD,
                        "preview": "Selected native thread",
                        "status": "idle",
                        "updatedAt": 200,
                    },
                    {
                        "id": "thread-other",
                        "cwd": "/workspace/other",
                        "preview": "Other native thread",
                        "status": "idle",
                        "updatedAt": 100,
                    },
                ],
                "nextCursor": None,
            },
            expected_params={"sortKey": "updated_at", "limit": 100},
        ),
    )
    selected_thread = {
        "thread": {
            "id": "thread-selected",
            "cwd": SYSTEM_CWD,
            "preview": "Selected native thread",
            "status": "idle",
        }
    }
    process.queue(
        "thread/resume",
        NativeStep(
            result=selected_thread,
            expected_params={
                "threadId": "thread-selected",
                "serviceName": "imcodex-system-test",
            },
        ),
        NativeStep(
            result=selected_thread,
            expected_params={
                "threadId": "thread-selected",
                "serviceName": "imcodex-system-test",
            },
        ),
    )
    process.queue(
        "turn/start",
        NativeStep(
            result={"turn": {"id": "turn-selected", "status": "inProgress"}},
            notifications=(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-selected",
                        "turnId": "turn-selected",
                        "item": {
                            "id": "selected-answer",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Continued the selected native thread",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-selected",
                        "turn": {"id": "turn-selected", "status": "completed"},
                    },
                },
            ),
            expected_params={
                "threadId": "thread-selected",
                "input": [
                    {
                        "type": "text",
                        "text": "continue on the selected thread",
                    }
                ],
                "summary": "concise",
            },
        ),
    )
    process.queue(
        "account/rateLimits/read",
        NativeStep(
            result={
                "rateLimits": {
                    "planType": "pro",
                    "credits": {
                        "hasCredits": True,
                        "unlimited": False,
                        "balance": "321",
                    },
                    "primary": {
                        "usedPercent": 20,
                        "windowDurationMins": 300,
                    },
                }
            },
            expects_no_params=True,
        ),
    )
    process.queue(
        "account/usage/read",
        NativeStep(
            result={
                "summary": {
                    "lifetimeTokens": 123456,
                    "currentStreakDays": 7,
                }
            },
            expects_no_params=True,
        ),
    )
    process.queue(
        "thread/start",
        NativeStep(
            result={
                "thread": {
                    "id": "thread-new",
                    "cwd": SYSTEM_CWD,
                    "preview": "Fresh native thread",
                    "status": "idle",
                }
            },
            expected_params={
                "cwd": SYSTEM_CWD,
                "serviceName": "imcodex-system-test",
                "dynamicTools": native_thread_dynamic_tool_specs(),
            },
        ),
    )
    harness = build_system_harness(tmp_path, process)
    driver = await _install_channel(harness, "telegram", tmp_path)

    try:
        await driver.send_text("201", "/threads")
        await driver.send_text("202", "/pick 1")
        await driver.send_text("203", "continue on the selected thread")
        await wait_until(
            lambda: "Continued the selected native thread"
            in driver.outbound_texts()
        )
        await _wait_for_native_turn_settled(harness, "thread-selected")
        await driver.send_text("204", "/credits")
        await driver.send_text("205", "/new")

        output = "\n".join(driver.outbound_texts())
        assert "Selected native thread" in output
        assert "Switched to Selected native thread." in output
        assert "Continued the selected native thread" in output
        assert "Plan: pro" in output
        assert "Credits: Available, balance 321" in output
        assert "Started thread thread-new." in output

        resumes = process.requests("thread/resume")
        assert [request["params"]["threadId"] for request in resumes] == [
            "thread-selected",
            "thread-selected",
        ]
        continuation = process.requests("turn/start")[0]
        assert continuation["params"]["threadId"] == "thread-selected"
        assert continuation["params"]["input"] == [
            {"type": "text", "text": "continue on the selected thread"}
        ]
        new_thread = process.requests("thread/start")[0]
        assert new_thread["params"]["cwd"] == SYSTEM_CWD
        assert harness.store.get_binding(
            driver.channel_id,
            driver.conversation_id,
        ).thread_id == "thread-new"
        process.assert_consumed()
    finally:
        await harness.close()

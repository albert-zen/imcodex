from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from imcodex.channels import (
    ChannelAccessPolicy,
    FEISHU_DOMAIN,
    LARK_DOMAIN,
    FeishuChannelAdapter,
)
from imcodex.channels.base import ChannelRouteContext
from imcodex.channels.feishu import FeishuImageReference
from imcodex.channels.media import (
    IMAGE_DOWNLOAD_FAILED,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    ImageTooLargeError,
    MediaDownloadError,
)
from imcodex.channels.middleware import UnifiedChannelMiddleware
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeFeishuSdk:
    def __init__(
        self,
        *,
        fail_connect: bool = False,
        send_success: bool = True,
        download_bytes: bytes | None = None,
    ) -> None:
        self.fail_connect = fail_connect
        self.send_success = send_success
        self.download_bytes = download_bytes
        self.handlers: dict[str, list] = {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.sent: list[tuple[str, dict, dict]] = []
        self.downloads: list[tuple[str, str, str]] = []
        self.bot_identity = SimpleNamespace(name="IMCodex")

    def on(self, name: str, handler):
        self.handlers.setdefault(name, []).append(handler)

        def unsubscribe() -> None:
            self.handlers[name].remove(handler)

        return unsubscribe

    async def connect_until_ready(self, *, timeout: float) -> None:
        self.connect_calls += 1
        if self.fail_connect:
            raise RuntimeError("connect failed")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def send(self, to: str, message: dict, opts: dict):
        self.sent.append((to, message, opts))
        return SimpleNamespace(success=self.send_success)

    async def download_resource(
        self,
        file_key: str,
        *,
        resource_type: str,
        message_id: str,
    ) -> bytes | None:
        self.downloads.append((file_key, resource_type, message_id))
        return self.download_bytes

    def connection_snapshot(self):
        return SimpleNamespace(state="connected", ready=True)


def _message(
    *,
    text: str = "inspect repo",
    chat_type: str = "p2p",
    thread_id: str | None = None,
    mentioned_bot: bool = False,
    raw_content_type: str = "text",
    resources: list[object] | None = None,
):
    return SimpleNamespace(
        id="om_1",
        message_id="om_1",
        raw_content_type=raw_content_type,
        content_text=text,
        resources=list(resources or []),
        mentioned_bot=mentioned_bot,
        conversation=SimpleNamespace(
            chat_id="oc_1",
            chat_type=chat_type,
            thread_id=thread_id,
        ),
        sender=SimpleNamespace(open_id="ou_owner"),
    )


def _image_resource(file_key: str) -> SimpleNamespace:
    return SimpleNamespace(type="image", file_key=file_key)


def _adapter(**kwargs) -> FeishuChannelAdapter:
    return FeishuChannelAdapter(
        enabled=True,
        app_id="cli_app",
        app_secret="secret",
        middleware=kwargs.pop("middleware", object()),
        access_policy=kwargs.pop("access_policy", ChannelAccessPolicy.allow_all()),
        channel_factory=kwargs.pop("channel_factory", lambda **_config: FakeFeishuSdk()),
        **kwargs,
    )


def _resource_downloader(sdk: FakeFeishuSdk):
    async def download(reference, write_chunk) -> None:
        sdk.downloads.append((reference.file_key, "image", reference.message_id))
        if sdk.download_bytes is None:
            raise MediaDownloadError
        await write_chunk(sdk.download_bytes)

    return download


def test_feishu_and_lark_domains_are_explicit() -> None:
    assert _adapter(domain="feishu").domain == FEISHU_DOMAIN
    assert _adapter(domain="lark").domain == LARK_DOMAIN
    with pytest.raises(ValueError, match="must be 'feishu' or 'lark'"):
        _adapter(domain="example.com")


def test_feishu_normalizes_direct_and_topic_messages() -> None:
    adapter = _adapter()

    direct = adapter.parse_inbound_message(_message())
    topic = adapter.parse_inbound_message(
        _message(
            text="@IMCodex inspect repo",
            chat_type="topic",
            thread_id="omt_root",
            mentioned_bot=True,
        )
    )

    assert direct == InboundMessage(
        channel_id="feishu",
        conversation_id="chat:oc_1",
        user_id="ou_owner",
        message_id="om_1",
        text="inspect repo",
    )
    assert topic is not None
    assert topic.conversation_id == "chat:oc_1:thread:omt_root"


def test_feishu_requires_group_mention_and_preserves_malformed_images() -> None:
    adapter = _adapter(require_mention=True)

    assert adapter.parse_inbound_message(_message(chat_type="group")) is None
    group_image = _message(
        chat_type="group",
        raw_content_type="image",
        text="![image](img_1)",
        resources=[_image_resource("img_1")],
    )
    assert adapter.parse_inbound_message(group_image) is None
    malformed = adapter._parse_inbound_message_with_images(
        _message(raw_content_type="image", text="![image]()")
    )
    assert malformed is not None
    assert malformed[0].text == ""
    assert len(malformed[1]) == 1
    assert malformed[1][0].file_key == ""
    assert adapter.parse_inbound_message(_message(raw_content_type="file")) is None


def test_feishu_normalizes_image_and_rich_post_resources_without_exposing_refs() -> None:
    adapter = _adapter(require_mention=True)
    adapter._sdk = FakeFeishuSdk()

    direct_image = _message(
        raw_content_type="image",
        text="![image](img_1)",
        resources=[
            _image_resource("img_1"),
            _image_resource("img_1"),
            SimpleNamespace(type="file", file_key="file_1"),
        ],
    )
    parsed_direct = adapter._parse_inbound_message_with_images(direct_image)

    assert parsed_direct is not None
    direct, direct_refs = parsed_direct
    assert direct.text == ""
    assert direct_refs[0].message_id == "om_1"
    assert [reference.file_key for reference in direct_refs] == ["img_1"]
    assert adapter.parse_inbound_message(direct_image) == direct

    post = _message(
        raw_content_type="post",
        text=(
            "@IMCodex inspect these\n\n"
            "![image](img_1)\n\n![image](img_2)"
        ),
        chat_type="group",
        thread_id="omt_root",
        mentioned_bot=True,
        resources=[
            _image_resource("img_1"),
            _image_resource("img_2"),
            _image_resource("img_1"),
        ],
    )
    parsed_post = adapter._parse_inbound_message_with_images(post)

    assert parsed_post is not None
    inbound, references = parsed_post
    assert inbound.text == "inspect these"
    assert inbound.conversation_id == "chat:oc_1:thread:omt_root"
    assert [reference.file_key for reference in references] == ["img_1", "img_2"]

    external_markdown = _message(
        raw_content_type="post",
        text="keep ![diagram](https://example.test/diagram.png)\n![image](img_1)",
        resources=[_image_resource("img_1")],
    )
    parsed_external = adapter._parse_inbound_message_with_images(external_markdown)
    assert parsed_external is not None
    assert parsed_external[0].text == "keep ![diagram](https://example.test/diagram.png)"

    partially_missing = _message(
        raw_content_type="post",
        text="![image](img_1)\n![image](missing)",
        resources=[_image_resource("img_1")],
    )
    parsed_missing = adapter._parse_inbound_message_with_images(partially_missing)
    assert parsed_missing is not None
    assert parsed_missing[0].text == ""
    assert [reference.file_key for reference in parsed_missing[1]] == [
        "img_1",
        "missing",
    ]


@pytest.mark.asyncio
async def test_feishu_recovers_content_v2_images_omitted_by_minimum_sdk() -> None:
    pipeline_module = pytest.importorskip(
        "lark_channel.channel.normalize.pipeline"
    )
    pipeline = pipeline_module.InboundPipeline(
        pipeline_module.PipelineConfig(),
        pipeline_module.PipelineDeps(),
    )
    sdk_message = await pipeline.normalize(
        message_event={
            "message_id": "om_v2",
            "create_time": "1",
            "chat_id": "oc_v2",
            "chat_type": "p2p",
            "message_type": "post",
            "content": {
                "zh_cn": {
                    "title": "",
                    "content_v2": [
                        [
                            {"tag": "text", "text": "look "},
                            {"tag": "img", "image_key": "img_v2_abc"},
                        ]
                    ],
                }
            },
        },
        sender={"sender_id": {"open_id": "ou_owner"}},
    )

    assert sdk_message is not None
    parsed = _adapter()._parse_inbound_message_with_images(sdk_message)

    assert parsed is not None
    inbound, references = parsed
    assert inbound.text == "look"
    assert inbound.conversation_id == "chat:oc_v2"
    assert [reference.file_key for reference in references] == ["img_v2_abc"]


@pytest.mark.asyncio
async def test_feishu_preserves_rendered_post_image_order_from_minimum_sdk() -> None:
    pipeline_module = pytest.importorskip(
        "lark_channel.channel.normalize.pipeline"
    )
    pipeline = pipeline_module.InboundPipeline(
        pipeline_module.PipelineConfig(),
        pipeline_module.PipelineDeps(),
    )
    sdk_message = await pipeline.normalize(
        message_event={
            "message_id": "om_order",
            "create_time": "1",
            "chat_id": "oc_order",
            "chat_type": "p2p",
            "message_type": "post",
            "content": {
                "zh_cn": {
                    "title": "",
                    "content": [
                        [
                            {"tag": "md", "text": "![image](img_A)"},
                            {"tag": "img", "image_key": "img_B"},
                        ]
                    ],
                }
            },
        },
        sender={"sender_id": {"open_id": "ou_owner"}},
    )

    assert sdk_message is not None
    # lark-channel-sdk 1.1.0 exposes resources in B,A order even though its
    # rendered post contains A,B. The adapter must follow rendered order.
    assert [resource.file_key for resource in sdk_message.resources] == [
        "img_B",
        "img_A",
    ]
    parsed = _adapter()._parse_inbound_message_with_images(sdk_message)

    assert parsed is not None
    inbound, references = parsed
    assert inbound.text == ""
    assert [reference.file_key for reference in references] == ["img_A", "img_B"]


@pytest.mark.asyncio
async def test_feishu_keeps_external_markdown_images_as_text() -> None:
    pipeline_module = pytest.importorskip(
        "lark_channel.channel.normalize.pipeline"
    )
    pipeline = pipeline_module.InboundPipeline(
        pipeline_module.PipelineConfig(),
        pipeline_module.PipelineDeps(),
    )
    markdown = "keep ![diagram](https://example.test/diagram.png)"
    sdk_message = await pipeline.normalize(
        message_event={
            "message_id": "om_external",
            "create_time": "1",
            "chat_id": "oc_external",
            "chat_type": "p2p",
            "message_type": "post",
            "content": {
                "zh_cn": {
                    "title": "",
                    "content": [[{"tag": "md", "text": markdown}]],
                }
            },
        },
        sender={"sender_id": {"open_id": "ou_owner"}},
    )

    assert sdk_message is not None
    assert [resource.file_key for resource in sdk_message.resources] == [
        "https://example.test/diagram.png"
    ]
    parsed = _adapter()._parse_inbound_message_with_images(sdk_message)

    assert parsed is not None
    inbound, references = parsed
    assert inbound.text == markdown
    assert references == ()


@pytest.mark.asyncio
async def test_feishu_strips_any_alt_text_for_platform_image_keys() -> None:
    pipeline_module = pytest.importorskip(
        "lark_channel.channel.normalize.pipeline"
    )
    pipeline = pipeline_module.InboundPipeline(
        pipeline_module.PipelineConfig(),
        pipeline_module.PipelineDeps(),
    )
    sdk_message = await pipeline.normalize(
        message_event={
            "message_id": "om_alt",
            "create_time": "1",
            "chat_id": "oc_alt",
            "chat_type": "p2p",
            "message_type": "post",
            "content": {
                "zh_cn": {
                    "title": "",
                    "content": [
                        [
                            {
                                "tag": "md",
                                "text": (
                                    "inspect ![diagram](img_v2_secret) please"
                                ),
                            }
                        ]
                    ],
                }
            },
        },
        sender={"sender_id": {"open_id": "ou_owner"}},
    )

    assert sdk_message is not None
    parsed = _adapter()._parse_inbound_message_with_images(sdk_message)

    assert parsed is not None
    inbound, references = parsed
    assert inbound.text == "inspect please"
    assert [reference.file_key for reference in references] == [
        "img_v2_secret"
    ]


def test_feishu_bounds_unique_image_references_after_deduplication() -> None:
    adapter = _adapter()
    resources = [_image_resource("img_0")]
    resources.extend(_image_resource(f"img_{index}") for index in range(MAX_IMAGE_COUNT + 2))
    parsed = adapter._parse_inbound_message_with_images(
        _message(
            raw_content_type="image",
            text="images",
            resources=resources,
        )
    )

    assert parsed is not None
    _, references = parsed
    assert len(references) == MAX_IMAGE_COUNT + 1
    assert [reference.file_key for reference in references].count("img_0") == 1


@pytest.mark.asyncio
async def test_feishu_sdk_callback_returns_immediately_and_dispatches_on_main_loop() -> None:
    class Middleware:
        def __init__(self) -> None:
            self.messages: list[InboundMessage] = []

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None) -> None:
            self.messages.append(inbound)

    sdk = FakeFeishuSdk()
    middleware = Middleware()
    adapter = _adapter(middleware=middleware, channel_factory=lambda **_config: sdk)

    await adapter.start()
    await asyncio.sleep(0)
    sdk.handlers["message"][0](_message())
    for _ in range(10):
        if middleware.messages:
            break
        await asyncio.sleep(0)
    await adapter.stop()

    assert [message.text for message in middleware.messages] == ["inspect repo"]
    assert sdk.connect_calls == 1
    assert sdk.disconnect_calls == 1


@pytest.mark.asyncio
async def test_feishu_image_download_is_lazy_and_uses_message_resource_api(
    tmp_path: Path,
) -> None:
    sdk = FakeFeishuSdk(download_bytes=_png_bytes())

    class Middleware:
        def __init__(self) -> None:
            self.messages: list[InboundMessage] = []
            self.completed = asyncio.Event()

        async def handle_inbound(
            self,
            _adapter,
            inbound,
            *,
            reply_to_message_id=None,
            prepare_inbound=None,
            pending_attachment_count=0,
        ) -> None:
            assert sdk.downloads == []
            assert pending_attachment_count == 1
            assert prepare_inbound is not None
            self.messages.append(await prepare_inbound(inbound))
            self.completed.set()

    middleware = Middleware()
    adapter = _adapter(
        middleware=middleware,
        channel_factory=lambda **_config: sdk,
        resource_downloader=_resource_downloader(sdk),
        media_dir=tmp_path / "media",
    )
    await adapter.start()
    await asyncio.sleep(0)

    sdk.handlers["message"][0](
        _message(
            raw_content_type="image",
            text="![image](img_1)",
            resources=[_image_resource("img_1")],
        )
    )
    await asyncio.wait_for(middleware.completed.wait(), timeout=1)
    await adapter.stop()

    assert sdk.downloads == [("img_1", "image", "om_1")]
    assert len(middleware.messages) == 1
    inbound = middleware.messages[0]
    assert inbound.text == ""
    assert inbound.input_error is None
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].content_type == "image/png"
    assert Path(inbound.attachments[0].local_path).is_file()


@pytest.mark.asyncio
async def test_feishu_production_download_uses_cancellable_token_http_flow() -> None:
    chunks_seen: list[bytes] = []
    requests: list[httpx.Request] = []

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _png_bytes()[:20]
            yield _png_bytes()[20:]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert request.headers["Accept-Encoding"] == "identity"
            assert request.read() == b'{"app_id":"cli_app","app_secret":"secret"}'
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-secret",
                    "expire": 7200,
                },
            )
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(_png_bytes()))},
            stream=ChunkedBody(),
        )

    sdk = FakeFeishuSdk(download_bytes=b"sdk bytes API must not be used")
    sdk._client = SimpleNamespace(config=object())

    async def write_chunk(chunk: bytes) -> None:
        chunks_seen.append(chunk)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        adapter._sdk = sdk
        await adapter._download_image(
            FeishuImageReference(message_id="om/id", file_key="img/key"),
            write_chunk,
        )

    assert b"".join(chunks_seen) == _png_bytes()
    assert sdk.downloads == []
    assert len(requests) == 2
    request = requests[1]
    assert request.method == "GET"
    assert request.url.raw_path == (
        b"/open-apis/im/v1/messages/om%2Fid/resources/img%2Fkey?type=image"
    )
    assert request.headers["Authorization"] == "Bearer tenant-secret"
    assert request.headers["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
async def test_feishu_cancellation_stops_inflight_token_http_request() -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open-apis/auth/v3/tenant_access_token/internal"
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled token request resumed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        task = asyncio.create_task(
            adapter._download_image(
                FeishuImageReference(message_id="om_1", file_key="img_1"),
                lambda _chunk: asyncio.sleep(0),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)


@pytest.mark.asyncio
async def test_feishu_token_http_flow_has_short_wall_clock_deadline(
    monkeypatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.feishu.FEISHU_TOKEN_DEADLINE_S", 0.01)

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("timed-out token request resumed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        with pytest.raises(MediaDownloadError):
            await asyncio.wait_for(
                adapter._get_tenant_access_token(),
                timeout=0.2,
            )


@pytest.mark.asyncio
async def test_feishu_download_rejects_oversized_length_before_streaming() -> None:
    iterated = False

    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal iterated
            iterated = True
            yield b"not reached"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)},
            stream=UnreadBody(),
        )

    sdk = FakeFeishuSdk()

    async def token_provider(_sdk: object) -> str:
        return "tenant-secret"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            http_client=client,
            tenant_token_provider=token_provider,
        )
        adapter._sdk = sdk
        with pytest.raises(ImageTooLargeError):
            await adapter._download_image(
                FeishuImageReference(message_id="om_1", file_key="img_1"),
                lambda _chunk: asyncio.sleep(0),
            )

    assert iterated is False
    assert sdk.downloads == []


@pytest.mark.asyncio
async def test_feishu_download_rejects_encoded_or_redirected_responses() -> None:
    requests: list[httpx.Request] = []
    response_status = 200

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if response_status == 302:
            return httpx.Response(302, headers={"Location": "https://example.test/leak"})
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=b"encoded",
        )

    sdk = FakeFeishuSdk()

    async def token_provider(_sdk: object) -> str:
        return "tenant-secret"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            http_client=client,
            tenant_token_provider=token_provider,
        )
        adapter._sdk = sdk
        reference = FeishuImageReference(message_id="om_1", file_key="img_1")
        with pytest.raises(MediaDownloadError):
            await adapter._download_image(reference, lambda _chunk: asyncio.sleep(0))
        response_status = 302
        with pytest.raises(MediaDownloadError):
            await adapter._download_image(reference, lambda _chunk: asyncio.sleep(0))

    assert len(requests) == 2
    assert all(request.url.host == "open.feishu.cn" for request in requests)


@pytest.mark.asyncio
async def test_feishu_stop_closes_only_owned_media_http_client() -> None:
    owned_adapter = _adapter()
    owned_client = owned_adapter._ensure_http_client()
    await owned_adapter.stop()
    assert owned_client.is_closed is True

    injected_client = httpx.AsyncClient()
    injected_adapter = _adapter(http_client=injected_client)
    await injected_adapter.stop()
    assert injected_client.is_closed is False
    await injected_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "resources", "download_bytes", "expected_downloads"),
    [
        (
            "![image](img_1)",
            [_image_resource("img_1")],
            None,
            [("img_1", "image", "om_1")],
        ),
        (
            "![image](img_1)",
            [],
            None,
            [("img_1", "image", "om_1")],
        ),
        (
            "![image](img_1)\n![image]()",
            [_image_resource("img_1")],
            _png_bytes(),
            [("img_1", "image", "om_1")],
        ),
    ],
)
async def test_feishu_image_download_failure_becomes_stable_input_error(
    tmp_path: Path,
    text: str,
    resources: list[object],
    download_bytes: bytes | None,
    expected_downloads: list[tuple[str, str, str]],
) -> None:
    sdk = FakeFeishuSdk(download_bytes=download_bytes)

    class Middleware:
        def __init__(self) -> None:
            self.message: InboundMessage | None = None
            self.completed = asyncio.Event()

        async def handle_inbound(
            self,
            _adapter,
            inbound,
            *,
            reply_to_message_id=None,
            prepare_inbound=None,
            pending_attachment_count=0,
        ) -> None:
            assert prepare_inbound is not None
            self.message = await prepare_inbound(inbound)
            self.completed.set()

    middleware = Middleware()
    adapter = _adapter(
        middleware=middleware,
        channel_factory=lambda **_config: sdk,
        resource_downloader=_resource_downloader(sdk),
        media_dir=tmp_path / "media",
    )
    await adapter.start()
    await asyncio.sleep(0)

    sdk.handlers["message"][0](
        _message(
            raw_content_type="image",
            text=text,
            resources=resources,
        )
    )
    await asyncio.wait_for(middleware.completed.wait(), timeout=1)
    await adapter.stop()

    assert middleware.message is not None
    assert middleware.message.input_error == IMAGE_DOWNLOAD_FAILED
    assert middleware.message.attachments == ()
    assert sdk.downloads == expected_downloads
    assert list((tmp_path / "media").glob("*")) == []


@pytest.mark.asyncio
async def test_feishu_sends_chunked_thread_replies() -> None:
    sdk = FakeFeishuSdk()
    adapter = _adapter(channel_factory=lambda **_config: sdk)
    adapter._sdk = sdk

    await adapter.send_message(
        OutboundMessage(
            channel_id="feishu",
            conversation_id="chat:oc_1:thread:omt_root",
            message_type="turn_result",
            text="a" * 3501,
            metadata={"reply_to_message_id": "om_1"},
        )
    )

    assert [len(item[1]["text"]) for item in sdk.sent] == [3500, 1]
    assert sdk.sent[0] == (
        "oc_1",
        {"text": "a" * 3500},
        {
            "receive_id_type": "chat_id",
            "reply_to": "om_1",
            "reply_in_thread": True,
        },
    )


@pytest.mark.asyncio
async def test_feishu_async_topic_output_uses_persisted_message_id_not_thread_id() -> None:
    sdk = FakeFeishuSdk()
    middleware = SimpleNamespace(
        get_route_context=lambda _channel_id, _conversation_id: ChannelRouteContext(
            admitted_user_id="ou_owner",
            last_inbound_message_id="om_last",
        )
    )
    adapter = _adapter(middleware=middleware, channel_factory=lambda **_config: sdk)
    adapter._sdk = sdk

    await adapter.send_message(
        OutboundMessage(
            channel_id="feishu",
            conversation_id="chat:oc_1:thread:omt_root",
            message_type="turn_result",
            text="done",
        )
    )

    assert sdk.sent[0][2]["reply_to"] == "om_last"
    assert sdk.sent[0][2]["reply_to"] != "omt_root"


@pytest.mark.asyncio
async def test_feishu_topic_output_fails_without_persisted_reply_message() -> None:
    sdk = FakeFeishuSdk()
    adapter = _adapter(channel_factory=lambda **_config: sdk)
    adapter._sdk = sdk

    with pytest.raises(RuntimeError, match="persisted inbound message ID"):
        await adapter.send_message(
            OutboundMessage(
                channel_id="feishu",
                conversation_id="chat:oc_1:thread:omt_root",
                message_type="turn_result",
                text="done",
            )
        )

    assert sdk.sent == []


@pytest.mark.asyncio
async def test_feishu_surfaces_outbound_rejection() -> None:
    sdk = FakeFeishuSdk(send_success=False)
    adapter = _adapter(channel_factory=lambda **_config: sdk)
    adapter._sdk = sdk

    with pytest.raises(RuntimeError, match="rejected an outbound message"):
        await adapter.send_message(
            OutboundMessage(
                channel_id="feishu",
                conversation_id="chat:oc_1",
                message_type="turn_result",
                text="done",
            )
        )


@pytest.mark.asyncio
async def test_feishu_rebuilds_sdk_after_initial_connection_failure() -> None:
    first = FakeFeishuSdk(fail_connect=True)
    second = FakeFeishuSdk()
    sdks = iter([first, second])
    delays: list[float] = []

    def factory(**_config):
        return next(sdks)

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    adapter = _adapter(channel_factory=factory, sleep=capture_sleep)
    await adapter.start()
    for _ in range(20):
        if second.connect_calls:
            break
        await asyncio.sleep(0)
    await adapter.stop()

    assert first.disconnect_calls == 1
    assert second.connect_calls == 1
    assert second.disconnect_calls == 1
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_feishu_start_requires_credentials() -> None:
    adapter = FeishuChannelAdapter(
        enabled=True,
        app_id="",
        app_secret="",
        middleware=object(),
    )

    with pytest.raises(RuntimeError, match="requires IMCODEX_FEISHU_APP_ID"):
        await adapter.start()


@pytest.mark.asyncio
async def test_feishu_immediate_stop_always_disconnects_sdk_and_stops_media(
    tmp_path: Path,
) -> None:
    sdk = FakeFeishuSdk()
    adapter = _adapter(
        channel_factory=lambda **_config: sdk,
        media_dir=tmp_path / "media",
    )

    await adapter.start()
    assert adapter.media_materializer._cleanup_task is not None
    await adapter.stop()

    assert sdk.disconnect_calls == 1
    assert adapter._sdk is None
    assert adapter.media_materializer._cleanup_task is None


@pytest.mark.asyncio
async def test_feishu_subscription_failure_disconnects_partial_sdk() -> None:
    class FailingSubscribeSdk(FakeFeishuSdk):
        def on(self, name: str, handler):
            if name == "reconnecting":
                raise RuntimeError("subscribe failed")
            return super().on(name, handler)

    sdk = FailingSubscribeSdk()
    adapter = _adapter(channel_factory=lambda **_config: sdk)

    with pytest.raises(RuntimeError, match="subscribe failed"):
        await adapter.start()

    assert sdk.disconnect_calls == 1
    assert adapter._sdk is None
    assert sdk.handlers["message"] == []


@pytest.mark.asyncio
async def test_feishu_serializes_inbound_messages_in_callback_order() -> None:
    class Middleware:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None) -> None:
            self.started.append(inbound.message_id)
            if inbound.message_id == "om_1":
                self.first_started.set()
                await self.release_first.wait()

    sdk = FakeFeishuSdk()
    middleware = Middleware()
    adapter = _adapter(middleware=middleware, channel_factory=lambda **_config: sdk)
    await adapter.start()
    await asyncio.sleep(0)

    sdk.handlers["message"][0](_message())
    second = _message(text="second")
    second.id = "om_2"
    second.message_id = "om_2"
    sdk.handlers["message"][0](second)

    await asyncio.wait_for(middleware.first_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert middleware.started == ["om_1"]

    middleware.release_first.set()
    for _ in range(20):
        if middleware.started == ["om_1", "om_2"]:
            break
        await asyncio.sleep(0)
    await adapter.stop()

    assert middleware.started == ["om_1", "om_2"]


@pytest.mark.asyncio
async def test_feishu_same_id_callbacks_deduplicate_before_second_download(
    tmp_path: Path,
) -> None:
    class Service:
        def __init__(self) -> None:
            self.store = ConversationStore(clock=lambda: 1.0)
            self.calls = 0

        async def handle_inbound(self, _inbound: InboundMessage):
            self.calls += 1
            return []

    sdk = FakeFeishuSdk(download_bytes=_png_bytes())
    service = Service()
    adapter = _adapter(
        middleware=UnifiedChannelMiddleware(service=service),
        channel_factory=lambda **_config: sdk,
        resource_downloader=_resource_downloader(sdk),
        media_dir=tmp_path / "media",
    )
    await adapter.start()
    await asyncio.sleep(0)
    callback = sdk.handlers["message"][0]
    replay = _message(
        raw_content_type="image",
        text="![image](img_1)",
        resources=[_image_resource("img_1")],
    )

    callback(replay)
    callback(replay)
    await asyncio.sleep(0)
    assert adapter._inbound_queue is not None
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)
    await adapter.stop()

    assert sdk.downloads == [("img_1", "image", "om_1")]
    assert service.calls == 1


@pytest.mark.asyncio
async def test_feishu_stop_drops_queued_inbound_before_sdk_disconnect() -> None:
    class BlockingDisconnectSdk(FakeFeishuSdk):
        def __init__(self) -> None:
            super().__init__()
            self.disconnect_started = asyncio.Event()
            self.release_disconnect = asyncio.Event()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.disconnect_started.set()
            await self.release_disconnect.wait()

    class Middleware:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None) -> None:
            self.started.append(inbound.message_id)
            if inbound.message_id == "om_1":
                self.first_started.set()
                await self.release_first.wait()

    sdk = BlockingDisconnectSdk()
    middleware = Middleware()
    adapter = _adapter(middleware=middleware, channel_factory=lambda **_config: sdk)
    await adapter.start()
    await asyncio.sleep(0)
    sdk.handlers["message"][0](_message())
    second = _message(text="second")
    second.id = "om_2"
    second.message_id = "om_2"
    sdk.handlers["message"][0](second)
    await asyncio.wait_for(middleware.first_started.wait(), timeout=1)

    stop_task = asyncio.create_task(adapter.stop())
    await asyncio.wait_for(sdk.disconnect_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert middleware.started == ["om_1"]
    sdk.release_disconnect.set()
    await stop_task
    assert middleware.started == ["om_1"]


@pytest.mark.asyncio
async def test_feishu_rejects_unauthorized_images_before_queueing_or_download(
    tmp_path: Path,
) -> None:
    sdk = FakeFeishuSdk(download_bytes=_png_bytes())
    adapter = _adapter(
        channel_factory=lambda **_config: sdk,
        access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"ou_owner"})),
        resource_downloader=_resource_downloader(sdk),
        media_dir=tmp_path / "media",
    )
    await adapter.start()
    await asyncio.sleep(0)
    intruder = _message(
        raw_content_type="image",
        text="![image](img_1)",
        resources=[_image_resource("img_1")],
    )
    intruder.sender.open_id = "ou_intruder"

    for _ in range(100):
        sdk.handlers["message"][0](intruder)
    await asyncio.sleep(0)

    assert adapter._inbound_queue is not None
    assert adapter._inbound_queue.qsize() == 0
    assert sdk.downloads == []
    await adapter.stop()


def test_feishu_sdk_is_created_with_strict_bounded_security(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Config:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class Channel:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    fake_module = SimpleNamespace(
        ChatQueueConfig=Config,
        DedupConfig=Config,
        FeishuChannel=Channel,
        InboundConfig=Config,
        MediaCapabilities=Config,
        PolicyConfig=Config,
        SafetyConfig=Config,
        SecurityConfig=Config,
        TransportConfig=Config,
    )
    monkeypatch.setitem(sys.modules, "lark_channel", fake_module)
    adapter = _adapter(channel_factory=None)

    adapter._create_sdk()

    security = captured["security"]
    assert security.mode == "strict"
    assert security.allow_insecure_ws is False
    assert security.allow_local_insecure_ws is False
    assert security.max_ws_fragment_parts == 64
    assert security.max_ws_fragment_bytes == 2 * 1024 * 1024
    assert security.max_concurrent_ws_handlers == 16
    assert security.resource_overflow_policy == "drop"
    media_capabilities = captured["inbound"].media_capabilities
    assert media_capabilities.image is True
    assert media_capabilities.audio is False
    assert media_capabilities.video is False
    assert media_capabilities.file is False
    assert media_capabilities.sticker is False
    dedup = captured["safety"].dedup
    assert dedup.enabled is False
    assert dedup.max_entries == 0


def test_feishu_sdk_zero_seen_cache_retains_no_ids() -> None:
    pytest.importorskip("lark_channel")
    from lark_channel.channel.safety import SeenCache

    seen = SeenCache(max_entries=0)
    seen.add_sync("om_1")

    assert seen.size() == 0
    assert seen.has_sync("om_1") is False


@pytest.mark.asyncio
async def test_feishu_media_auth_caches_tenant_token_until_refresh_window() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(http_client=client)
        assert await adapter._get_tenant_access_token() == "tenant-token"
        assert await adapter._get_tenant_access_token() == "tenant-token"

    assert requests == 1


def test_feishu_real_optional_sdk_construction_smoke() -> None:
    pytest.importorskip("lark_channel")
    adapter = FeishuChannelAdapter(
        enabled=True,
        app_id="cli_test",
        app_secret="secret",
        middleware=object(),
        access_policy=ChannelAccessPolicy.allow_all(),
    )

    sdk = adapter._create_sdk()
    try:
        security = sdk.config.security
        assert security.mode == "strict"
        assert security.allow_insecure_ws is False
        assert security.allow_local_insecure_ws is False
        media_capabilities = sdk.config.inbound.media_capabilities
        assert media_capabilities.image is True
        assert media_capabilities.audio is False
        assert media_capabilities.video is False
        assert media_capabilities.file is False
        assert media_capabilities.sticker is False
        assert sdk.config.safety.dedup.enabled is False
        assert sdk.config.safety.dedup.max_entries == 0
    finally:
        sdk.stop(join_timeout=0.1)

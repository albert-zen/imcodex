from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from imcodex.channels import (
    ChannelAccessPolicy,
    FEISHU_DOMAIN,
    LARK_DOMAIN,
    FeishuChannelAdapter,
)
from imcodex.channels.base import ChannelRouteContext
from imcodex.models import InboundMessage, OutboundMessage


class FakeFeishuSdk:
    def __init__(self, *, fail_connect: bool = False, send_success: bool = True) -> None:
        self.fail_connect = fail_connect
        self.send_success = send_success
        self.handlers: dict[str, list] = {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.sent: list[tuple[str, dict, dict]] = []
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

    def connection_snapshot(self):
        return SimpleNamespace(state="connected", ready=True)


def _message(
    *,
    text: str = "inspect repo",
    chat_type: str = "p2p",
    thread_id: str | None = None,
    mentioned_bot: bool = False,
):
    return SimpleNamespace(
        id="om_1",
        message_id="om_1",
        raw_content_type="text",
        content_text=text,
        mentioned_bot=mentioned_bot,
        conversation=SimpleNamespace(
            chat_id="oc_1",
            chat_type=chat_type,
            thread_id=thread_id,
        ),
        sender=SimpleNamespace(open_id="ou_owner"),
    )


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


def test_feishu_requires_group_mention_and_ignores_non_text() -> None:
    adapter = _adapter(require_mention=True)

    assert adapter.parse_inbound_message(_message(chat_type="group")) is None
    non_text = _message()
    non_text.raw_content_type = "image"
    assert adapter.parse_inbound_message(non_text) is None


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
async def test_feishu_immediate_stop_always_disconnects_sdk() -> None:
    sdk = FakeFeishuSdk()
    adapter = _adapter(channel_factory=lambda **_config: sdk)

    await adapter.start()
    await adapter.stop()

    assert sdk.disconnect_calls == 1
    assert adapter._sdk is None


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
async def test_feishu_rejects_unauthorized_messages_before_queueing() -> None:
    sdk = FakeFeishuSdk()
    adapter = _adapter(
        channel_factory=lambda **_config: sdk,
        access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"ou_owner"})),
    )
    await adapter.start()
    await asyncio.sleep(0)
    intruder = _message()
    intruder.sender.open_id = "ou_intruder"

    for _ in range(100):
        sdk.handlers["message"][0](intruder)
    await asyncio.sleep(0)

    assert adapter._inbound_queue is not None
    assert adapter._inbound_queue.qsize() == 0
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
    finally:
        sdk.stop(join_timeout=0.1)

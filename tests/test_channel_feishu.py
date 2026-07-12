from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from imcodex.channels import (
    ChannelAccessPolicy,
    FEISHU_DOMAIN,
    LARK_DOMAIN,
    FeishuChannelAdapter,
)
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

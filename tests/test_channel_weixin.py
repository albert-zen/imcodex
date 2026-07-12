from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from imcodex.channels import ChannelAccessPolicy, WeixinChannelAdapter
from imcodex.channels.weixin_ilink import ILinkError
from imcodex.channels.weixin_state import (
    WeixinCredentials,
    WeixinStateStore,
    WeixinTransportState,
)
from imcodex.models import InboundMessage, OutboundMessage


def _raw_message(
    *,
    user_id: str = "owner@im.wechat",
    message_id: int = 123,
    text: str = "inspect repo",
    context_token: str = "context-secret",
) -> dict:
    return {
        "message_id": message_id,
        "from_user_id": user_id,
        "message_type": 1,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        "context_token": context_token,
    }


class FakeTransport:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.sent: list[dict] = []
        self.notify_start_calls = 0
        self.notify_stop_calls = 0
        self.close_calls = 0
        self.poll_calls: list[tuple[str, int]] = []
        self.poll_started = asyncio.Event()
        self.block_when_empty = asyncio.Event()

    async def notify_start(self) -> dict:
        self.notify_start_calls += 1
        return {"ret": 0}

    async def notify_stop(self) -> dict:
        self.notify_stop_calls += 1
        return {"ret": 0}

    async def close(self) -> None:
        self.close_calls += 1

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int) -> dict:
        self.poll_calls.append((get_updates_buf, timeout_ms))
        self.poll_started.set()
        if self.responses:
            return self.responses.pop(0)
        await self.block_when_empty.wait()
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}

    async def send_text(self, **payload) -> str:
        self.sent.append(payload)
        return "out-1"


def _credentials() -> WeixinCredentials:
    return WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        owner_user_id="owner@im.wechat",
    )


def _adapter(tmp_path: Path, **kwargs) -> WeixinChannelAdapter:
    store = kwargs.pop("state_store", WeixinStateStore(tmp_path))
    if store.load_credentials() is None:
        store.save_credentials(_credentials())
    transport = kwargs.pop("transport", FakeTransport())
    return WeixinChannelAdapter(
        enabled=True,
        middleware=kwargs.pop("middleware", object()),
        state_dir=tmp_path,
        state_store=store,
        transport_factory=kwargs.pop("transport_factory", lambda _credentials: transport),
        access_policy=kwargs.pop(
            "access_policy",
            ChannelAccessPolicy(allowed_user_ids=frozenset({"owner@im.wechat"})),
        ),
        **kwargs,
    )


def test_weixin_normalizes_text_only_direct_messages(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)

    inbound = adapter.parse_inbound_message(_raw_message())
    bot_message = {**_raw_message(), "message_type": 2}
    group_message = {**_raw_message(), "group_id": "group-1"}
    image_message = {**_raw_message(), "item_list": [{"type": 2}]}
    malformed_message = {**_raw_message(), "message_type": "invalid"}

    assert inbound == InboundMessage(
        channel_id="weixin",
        conversation_id="user:owner@im.wechat",
        user_id="owner@im.wechat",
        message_id="123",
        text="inspect repo",
    )
    assert adapter.parse_inbound_message(bot_message) is None
    assert adapter.parse_inbound_message(group_message) is None
    assert adapter.parse_inbound_message(image_message) is None
    assert adapter.parse_inbound_message(malformed_message) is None


@pytest.mark.asyncio
async def test_weixin_does_not_persist_context_for_unauthorized_sender(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)

    await adapter.handle_raw_message(
        _raw_message(
            user_id="intruder@im.wechat",
            context_token="intruder-context",
        )
    )

    assert "intruder@im.wechat" not in adapter._state.context_tokens


@pytest.mark.asyncio
async def test_weixin_poll_persists_context_before_dispatch_and_then_cursor(
    tmp_path: Path,
) -> None:
    class Middleware:
        def __init__(self) -> None:
            self.messages: list[InboundMessage] = []

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None) -> None:
            self.messages.append(inbound)

    transport = FakeTransport(
        [
            {
                "ret": 0,
                "msgs": [_raw_message()],
                "get_updates_buf": "next-cursor",
                "longpolling_timeout_ms": 20_000,
            }
        ]
    )
    middleware = Middleware()
    adapter = _adapter(tmp_path, transport=transport, middleware=middleware)
    adapter._transport = transport
    adapter._state = WeixinTransportState(get_updates_buf="old-cursor")

    await adapter._poll_once()

    persisted = adapter.state_store.load_transport_state()
    assert [message.text for message in middleware.messages] == ["inspect repo"]
    assert persisted.get_updates_buf == "next-cursor"
    assert persisted.context_tokens == {"owner@im.wechat": "context-secret"}
    assert adapter.poll_timeout_ms == 20_000


@pytest.mark.asyncio
async def test_weixin_start_defaults_access_to_qr_login_owner_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    adapter = _adapter(
        tmp_path,
        transport=transport,
        access_policy=ChannelAccessPolicy(
            allowed_user_ids=frozenset(),
            allowed_conversation_ids=frozenset({"user:owner@im.wechat"}),
        ),
    )

    await adapter.start()
    await asyncio.wait_for(transport.poll_started.wait(), timeout=1)
    await adapter.stop()

    assert adapter.access_policy.allows(
        user_id="owner@im.wechat",
        conversation_id="user:owner@im.wechat",
    )
    assert not adapter.access_policy.allows(
        user_id="intruder@im.wechat",
        conversation_id="user:intruder@im.wechat",
    )
    assert adapter.access_policy.allowed_conversation_ids == frozenset({"user:owner@im.wechat"})
    assert transport.notify_start_calls == 1
    assert transport.notify_stop_calls == 1
    assert transport.close_calls == 1


@pytest.mark.asyncio
async def test_weixin_discards_transport_state_from_a_different_account(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path)
    store.save_credentials(_credentials())
    store.save_transport_state(
        WeixinTransportState(
            account_id="old-bot@im.bot",
            get_updates_buf="old-cursor",
            context_tokens={"owner@im.wechat": "old-context"},
        )
    )
    transport = FakeTransport()
    adapter = _adapter(tmp_path, state_store=store, transport=transport)

    await adapter.start()
    await asyncio.wait_for(transport.poll_started.wait(), timeout=1)
    await adapter.stop()

    state = store.load_transport_state()
    assert state.account_id == "bot@im.bot"
    assert state.get_updates_buf == ""
    assert state.context_tokens == {}


@pytest.mark.asyncio
async def test_weixin_prunes_context_tokens_revoked_by_current_allowlist(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path)
    store.save_credentials(_credentials())
    store.save_transport_state(
        WeixinTransportState(
            account_id="bot@im.bot",
            context_tokens={
                "owner@im.wechat": "owner-context",
                "intruder@im.wechat": "intruder-context",
            },
        )
    )
    transport = FakeTransport()
    adapter = _adapter(tmp_path, state_store=store, transport=transport)

    await adapter.start()
    await asyncio.wait_for(transport.poll_started.wait(), timeout=1)
    await adapter.stop()

    assert store.load_transport_state().context_tokens == {"owner@im.wechat": "owner-context"}


@pytest.mark.asyncio
async def test_weixin_sends_chunked_text_with_persisted_context(tmp_path: Path) -> None:
    transport = FakeTransport()
    adapter = _adapter(tmp_path, transport=transport)
    adapter._transport = transport
    adapter._state.set_context_token("owner@im.wechat", "context-secret")

    await adapter.send_message(
        OutboundMessage(
            channel_id="weixin",
            conversation_id="user:owner@im.wechat",
            message_type="turn_result",
            text="a" * 4001,
        )
    )

    assert [len(item["text"]) for item in transport.sent] == [4000, 1]
    assert {item["context_token"] for item in transport.sent} == {"context-secret"}
    assert {item["to_user_id"] for item in transport.sent} == {"owner@im.wechat"}
    assert [item["client_id"] for item in transport.sent] == [None, None]


@pytest.mark.asyncio
async def test_weixin_uses_stable_delivery_id_for_chunk_retries(tmp_path: Path) -> None:
    transport = FakeTransport()
    adapter = _adapter(tmp_path, transport=transport)
    adapter._transport = transport
    adapter._state.set_context_token("owner@im.wechat", "context-secret")

    await adapter.send_message(
        OutboundMessage(
            channel_id="weixin",
            conversation_id="user:owner@im.wechat",
            message_type="turn_result",
            text="a" * 4001,
            metadata={"delivery_id": "imcodex:stable"},
        )
    )

    assert [item["client_id"] for item in transport.sent] == [
        "imcodex:stable:0",
        "imcodex:stable:1",
    ]


@pytest.mark.asyncio
async def test_weixin_refuses_outbound_without_context_token(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    adapter._transport = FakeTransport()

    with pytest.raises(RuntimeError, match="active context token"):
        await adapter.send_message(
            OutboundMessage(
                channel_id="weixin",
                conversation_id="user:owner@im.wechat",
                message_type="turn_result",
                text="done",
            )
        )


@pytest.mark.asyncio
async def test_weixin_marks_stale_credentials_without_logging_tokens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setattr("imcodex.channels.weixin.emit_event", lambda **payload: events.append(payload))
    transport = FakeTransport([{"ret": -14, "errmsg": "stale"}])
    adapter = _adapter(tmp_path, transport=transport)
    adapter._transport = transport

    with pytest.raises(ILinkError) as exc_info:
        await adapter._poll_once()
    adapter._mark_stale_token()

    assert exc_info.value.code == -14
    assert adapter._auth_stale is True
    assert events[0]["event"] == "weixin.credentials.stale"
    assert "bot-secret" not in str(events)


@pytest.mark.asyncio
async def test_weixin_start_requires_qr_login_state(tmp_path: Path) -> None:
    adapter = WeixinChannelAdapter(
        enabled=True,
        middleware=object(),
        state_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="channels login weixin"):
        await adapter.start()

from __future__ import annotations

import pytest

from imcodex.channels import ChannelAccessPolicy, parse_id_set, split_text
from imcodex.channels.base import BaseChannelAdapter
from imcodex.models import InboundMessage, OutboundMessage


def test_parse_id_set_accepts_comma_and_newline_separated_values() -> None:
    assert parse_id_set("u1, u2\nu3") == frozenset({"u1", "u2", "u3"})


def test_access_policy_denies_empty_user_allowlist() -> None:
    policy = ChannelAccessPolicy.from_config({})

    assert policy.allows(user_id="u1", conversation_id="chat:1") is False


def test_access_policy_requires_user_and_optional_conversation_match() -> None:
    policy = ChannelAccessPolicy(
        allowed_user_ids=frozenset({"u1"}),
        allowed_conversation_ids=frozenset({"chat:1"}),
    )

    assert policy.allows(user_id="u1", conversation_id="chat:1") is True
    assert policy.allows(user_id="u2", conversation_id="chat:1") is False
    assert policy.allows(user_id="u1", conversation_id="chat:2") is False


def test_split_text_prefers_soft_boundaries_and_hard_splits_long_tokens() -> None:
    assert split_text("alpha beta gamma", limit=10) == ["alpha beta", "gamma"]
    assert split_text("abcdefghijk", limit=5) == ["abcde", "fghij", "k"]
    assert split_text("你好世界", limit=2) == ["你好", "世界"]


@pytest.mark.asyncio
async def test_base_adapter_drops_unauthorized_inbound_before_middleware(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("imcodex.channels.base.emit_event", lambda **payload: events.append(payload))

    class Middleware:
        def __init__(self) -> None:
            self.calls = 0

        async def handle_inbound(self, *_args, **_kwargs) -> None:
            self.calls += 1

    class Adapter(BaseChannelAdapter):
        channel_id = "test"

        @classmethod
        def from_config(cls, *, config, middleware):
            raise NotImplementedError

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send_message(self, message: OutboundMessage) -> None:
            return None

    middleware = Middleware()
    adapter = Adapter(
        middleware=middleware,
        access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"owner"})),
    )

    await adapter.dispatch_inbound(
        InboundMessage(
            channel_id="test",
            conversation_id="chat:1",
            user_id="intruder",
            message_id="m1",
            text="/cwd /tmp",
        )
    )

    assert middleware.calls == 0
    assert events[0]["event"] == "message.inbound.access_denied"

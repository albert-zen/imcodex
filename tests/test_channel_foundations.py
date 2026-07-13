from __future__ import annotations

from types import SimpleNamespace

import pytest

from imcodex.channels import ChannelAccessPolicy, parse_id_set, split_text
from imcodex.channels.base import BaseChannelAdapter, ChannelRouteContext
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
async def test_base_adapter_drops_unauthorized_inbound_before_middleware(
    monkeypatch,
) -> None:
    events: list[dict] = []
    health: list[tuple[str, dict]] = []
    monkeypatch.setattr("imcodex.channels.base.emit_event", lambda **payload: events.append(payload))
    monkeypatch.setattr(
        "imcodex.channels.base.mark_channel_health",
        lambda channel_id, **payload: health.append((channel_id, payload)),
    )

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
    assert health[0][0] == "test"
    assert health[0][1]["inbound_access_ready"] is True
    assert health[0][1]["access_policy_mode"] == "restricted"
    assert health[0][1]["last_inbound_access_denial_reason"] == "user_or_conversation_not_allowed"
    assert health[0][1]["last_inbound_access_denied_at"].endswith("+00:00")


def test_access_policy_health_exposes_deny_all_without_identifiers() -> None:
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

    adapter = Adapter(middleware=object())

    assert adapter.inbound_access_ready is False
    assert adapter.access_policy_health() == {
        "inbound_access_ready": False,
        "access_policy_mode": "deny_all",
        "allowed_user_count": 0,
        "allowed_conversation_count": 0,
    }


def test_access_policy_health_stays_restricted_when_only_users_are_wildcarded() -> None:
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

    adapter = Adapter(
        middleware=object(),
        access_policy=ChannelAccessPolicy(
            allowed_user_ids=frozenset({"*"}),
            allowed_conversation_ids=frozenset({"conversation-1"}),
        ),
    )

    assert adapter.access_policy_health()["access_policy_mode"] == "restricted"


@pytest.mark.asyncio
async def test_access_denial_diagnostics_are_rate_limited(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("imcodex.channels.base.emit_event", lambda **payload: events.append(payload))

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

    adapter = Adapter(middleware=object())
    for index in range(25):
        await adapter.dispatch_inbound(
            InboundMessage(
                channel_id="test",
                conversation_id="chat:1",
                user_id=f"intruder-{index}",
                message_id=f"m{index}",
                text="hello",
            )
        )

    assert len(events) == 10


def test_outbound_gate_rechecks_persisted_sender_against_current_policy() -> None:
    middleware = SimpleNamespace(
        get_route_context=lambda _channel_id, _conversation_id: ChannelRouteContext(
            admitted_user_id="owner",
            last_inbound_message_id="m1",
        )
    )

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

    adapter = Adapter(
        middleware=middleware,
        access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"owner"})),
    )
    message = OutboundMessage(
        channel_id="test",
        conversation_id="chat:1",
        message_type="turn_result",
        text="done",
    )

    adapter.ensure_outbound_allowed(message)
    adapter.access_policy = ChannelAccessPolicy(allowed_user_ids=frozenset({"someone-else"}))

    with pytest.raises(PermissionError, match="current access policy"):
        adapter.ensure_outbound_allowed(message)

    message.metadata["user_id"] = "someone-else"
    with pytest.raises(PermissionError, match="current access policy"):
        adapter.ensure_outbound_allowed(message)

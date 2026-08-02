from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from imagent.bindings import InMemoryBindingRepository
from imagent.contracts import (
    AttachmentContent,
    DeliveryIntent,
    LocalPath,
    TextContent,
)
from imagent.gateway import ImAgentGateway
from imagent.proactive_delivery import ScopedDeliveryAuthorizer
from imagent.testing import FakeAgentApplicationAdapter, FakeChannelAdapter

from imcodex.bridge.sdk_delivery import (
    ImcodexDeliveryOutcomeObserver,
    ImcodexProactiveDelivery,
)
from imcodex.channels.sdk_webhook import SdkWebhookChannel
from imcodex.models import OutboundMessage


class _ProductDelivery:
    def resolve_outbound_route(self, thread_id: str):
        assert thread_id == "thread-1"
        return "telegram", "chat-1"


class _Stager:
    def __init__(self) -> None:
        self.released = None
        self.referenced = None

    def release(self, artifacts) -> None:
        self.released = tuple(artifacts)

    def cleanup_unreferenced(self, referenced) -> None:
        self.referenced = referenced


@pytest.mark.asyncio
async def test_product_proactive_delivery_uses_sdk_gateway() -> None:
    channel = FakeChannelAdapter("telegram")
    authorizer = ScopedDeliveryAuthorizer()
    gateway = ImAgentGateway(
        channels=[channel],
        applications=[FakeAgentApplicationAdapter()],
        bindings=InMemoryBindingRepository(),
        delivery_authorizer=authorizer,
    )
    delivery = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=authorizer,
        product_service=_ProductDelivery(),
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram", "qq", "feishu", "weixin"},
        webhook_channel=SdkWebhookChannel(),
    )
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="tool_delivery",
        text="Done",
        metadata={"delivery_id": "delivery-1"},
    )

    outbound, delivered, durable = await delivery.deliver_outbound_message(message)

    assert outbound == [message]
    assert delivered is True
    assert durable is True
    assert len(channel.sent) == 1
    assert channel.sent[0].conversation_ref.native_conversation_id == "chat-1"
    assert channel.sent[0].content == (TextContent("Done"),)


@pytest.mark.asyncio
async def test_delivery_outcome_observer_releases_local_paths() -> None:
    stager = _Stager()
    store = SimpleNamespace(
        referenced_terminal_artifact_paths=lambda: {"preserve-old"}
    )
    observer = ImcodexDeliveryOutcomeObserver(
        artifact_stager=stager,
        product_store=store,
    )
    intent = DeliveryIntent(
        delivery_id="delivery-1",
        target=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
                metadata={"sha256": "a" * 64},
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(intent, result=None, error=None)

    assert stager.released == ("/spool/result.txt",)
    assert stager.referenced == {"preserve-old"}

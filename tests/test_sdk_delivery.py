from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from imagent.bindings import InMemoryBindingRepository
from imagent.contracts import (
    AttachmentContent,
    DeliveryItemReceipt,
    DeliveryItemStatus,
    DeliveryReceipt,
    DeliveryReceiptStatus,
    LocalPath,
    TextContent,
)
from imagent.contracts import (
    OutboundMessage as SdkOutboundMessage,
)
from imagent.delivery_outcomes import DeliveryOutcome, DeliveryOutcomeContext
from imagent.gateway import GatewayRepositories, ImAgentGateway
from imagent.proactive_delivery import ScopedDeliveryAuthorizer
from imagent.testing import FakeAgentApplicationAdapter, FakeChannelAdapter

from imcodex.bridge.sdk_delivery import (
    ImcodexDeliveryOutcomeObserver,
    ImcodexProactiveDelivery,
)
from imcodex.channels.sdk_webhook import SdkWebhookChannel
from imcodex.models import OutboundMessage


class _ProductStore:
    def find_recipient_route_by_thread_id(self, thread_id: str):
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


class _Ledger:
    def __init__(self) -> None:
        self.completed = None
        self.transferred = None

    def transfer(self, delivery_id, artifacts) -> None:
        self.transferred = (delivery_id, tuple(artifacts))

    def complete_attempt(self, attempt_id, paths) -> None:
        self.completed = (attempt_id, tuple(paths))

    def complete_delivery(self, delivery_id, paths) -> None:
        self.completed = (delivery_id, tuple(paths))

    async def stage_upload(self, content, **kwargs):
        raise AssertionError("this test has no uploads")

    def discard_request(self, artifacts) -> None:
        return None


@pytest.mark.asyncio
async def test_product_proactive_delivery_uses_sdk_gateway() -> None:
    channel = FakeChannelAdapter("telegram")
    authorizer = ScopedDeliveryAuthorizer()
    gateway = ImAgentGateway(
        channels=[channel],
        applications=[FakeAgentApplicationAdapter()],
        repositories=GatewayRepositories(bindings=InMemoryBindingRepository()),
        delivery_authorizer=authorizer,
    )
    delivery = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=authorizer,
        product_store=_ProductStore(),
        artifact_ledger=_Ledger(),
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
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(
        artifact_ledger=ledger,
    )
    message = SdkOutboundMessage(
        delivery_id="delivery-1",
        conversation_ref=SimpleNamespace(),
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

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM)
        ),
    )

    assert ledger.completed == ("delivery-1", ("/spool/result.txt",))


@pytest.mark.asyncio
async def test_delivery_outcome_observer_retains_lease_for_retryable_attempt() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(
        artifact_ledger=ledger,
    )
    message = SdkOutboundMessage(
        delivery_id="delivery-retryable",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(status=DeliveryReceiptStatus.RETRYABLE_FAILURE)
        ),
    )

    assert ledger.completed is None


@pytest.mark.asyncio
async def test_delivery_outcome_observer_releases_only_terminal_partial_items() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(artifact_ledger=ledger)
    message = SdkOutboundMessage(
        delivery_id="delivery-partial",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="accepted",
                media_type="text/plain",
                source=LocalPath("/spool/accepted.txt"),
            ),
            AttachmentContent(
                attachment_id="retryable",
                media_type="text/plain",
                source=LocalPath("/spool/retryable.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(
                status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM,
                items=(
                    DeliveryItemReceipt(0, DeliveryItemStatus.ACCEPTED, "accepted"),
                    DeliveryItemReceipt(
                        1,
                        DeliveryItemStatus.RETRYABLE_FAILURE,
                        "retryable",
                    ),
                ),
            )
        ),
    )

    assert ledger.completed == (
        "delivery-partial",
        ("/spool/accepted.txt",),
    )

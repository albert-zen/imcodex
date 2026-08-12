from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from imagent import ProjectionPolicy
from imagent.applications import ProjectRef, ThreadRef
from imagent.gateway.delivery import (
    DeliverySubmissionState,
    ScopedDeliveryAuthorizer,
    ThreadRouteDeliveryTarget,
)
from imagent.interaction.messages import (
    ConversationRef,
    OutboundMessage as SdkOutboundMessage,
    TextContent,
)

from imcodex.channels.sdk_webhook import SdkRuntimeService, SdkWebhookChannel
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.sdk_composition import SDK_PROJECTION_POLICY


class _Gateway:
    def __init__(self, authorizer: ScopedDeliveryAuthorizer) -> None:
        self.authorizer = authorizer
        self.intent = None
        self.principal = None

    async def deliver_proactively(self, intent, *, credential):
        self.intent = intent
        self.principal = await self.authorizer.authenticate(credential)
        return SimpleNamespace(state=DeliverySubmissionState.ACCEPTED, error=None)


def test_sdk_composition_remembers_last_thread_recipient() -> None:
    assert SDK_PROJECTION_POLICY is ProjectionPolicy.REMEMBERED_LAST_RECIPIENT


async def test_current_thread_delivery_uses_sdk_thread_route_target() -> None:
    authorizer = ScopedDeliveryAuthorizer()
    gateway = _Gateway(authorizer)
    project_ref = ProjectRef("codex-main", "imcodex-workspace")
    service = SdkRuntimeService(
        channel=SdkWebhookChannel(),
        gateway=gateway,
        client=SimpleNamespace(),
        product_state=SimpleNamespace(),
        project_ref=project_ref,
        delivery_authorizer=authorizer,
    )
    message = OutboundMessage(
        channel_id="",
        conversation_id="",
        message_type="tool_delivery",
        text="artifact ready",
        metadata={
            "delivery_id": "delivery-1",
            "source_thread_id": "thread-1",
        },
    )

    outbound, delivered, durable = await service.deliver_outbound_message(message)

    thread_ref = ThreadRef(project_ref, "thread-1")
    assert isinstance(gateway.intent.target, ThreadRouteDeliveryTarget)
    assert gateway.intent.target.thread_ref == thread_ref
    assert gateway.principal.allowed_threads == (thread_ref,)
    assert gateway.principal.allowed_conversations == ()
    assert outbound == [message]
    assert delivered is True
    assert durable is True


async def test_webhook_receive_round_trips_product_route_and_reply() -> None:
    channel = SdkWebhookChannel()

    async def on_message(message):
        await channel.send(
            SdkOutboundMessage(
                delivery_id="reply-1",
                conversation_ref=message.conversation_ref,
                content=(TextContent("hello back"),),
                created_at=datetime.now(UTC),
                reply_to=message.message_id,
            )
        )

    await channel.start(on_message)
    try:
        outputs = await channel.receive(
            InboundMessage(
                message_id="incoming-1",
                channel_id="telegram",
                conversation_id="chat-7",
                user_id="user-2",
                text="hello",
            )
        )
    finally:
        await channel.stop()

    assert len(outputs) == 1
    assert outputs[0].channel_id == "telegram"
    assert outputs[0].conversation_id == "chat-7"
    assert outputs[0].text == "hello back"
    assert outputs[0].metadata["delivery_id"] == "reply-1"


async def test_webhook_synchronous_delivery_deduplicates_delivery_id() -> None:
    channel = SdkWebhookChannel()

    async def on_message(message):
        outbound = SdkOutboundMessage(
            delivery_id="same-delivery",
            conversation_ref=message.conversation_ref,
            content=(TextContent("one copy"),),
            created_at=datetime.now(UTC),
            reply_to=message.message_id,
        )
        await channel.send(outbound)
        await channel.send(outbound)

    await channel.start(on_message)
    try:
        outputs = await channel.receive(
            InboundMessage(
                message_id="incoming-2",
                channel_id="webhook",
                conversation_id="conversation-2",
                user_id="user-2",
                text="hello",
            )
        )
    finally:
        await channel.stop()

    assert [item.metadata["delivery_id"] for item in outputs] == ["same-delivery"]


def test_native_channel_route_is_not_decoded_as_webhook_namespace() -> None:
    assert SdkWebhookChannel._product_route(ConversationRef("telegram", "chat-9")) == (
        "telegram",
        "chat-9",
    )

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from imagent.contracts import OutboundMessage as SdkOutboundMessage
from imagent.contracts import TextContent

from imcodex.channels.sdk_webhook import SdkWebhookChannel
from imcodex.models import InboundMessage


class _OutboundSink:
    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, message) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_webhook_channel_round_trips_dynamic_namespace_and_immediate_reply() -> None:
    channel = SdkWebhookChannel()

    async def on_message(message) -> None:
        await channel.send(
            SdkOutboundMessage(
                delivery_id="delivery-1",
                conversation_ref=message.conversation_ref,
                content=(TextContent("Done"),),
                created_at=datetime.now(UTC),
                reply_to=message.message_id,
            )
        )

    await channel.start(on_message, lambda operation: None)
    try:
        outputs = await channel.receive(
            InboundMessage(
                channel_id="custom-a",
                conversation_id="room/1",
                user_id="user-1",
                message_id="message-1",
                text="hello",
            )
        )
    finally:
        await channel.stop()

    assert len(outputs) == 1
    assert outputs[0].channel_id == "custom-a"
    assert outputs[0].conversation_id == "room/1"
    assert outputs[0].text == "Done"
    assert outputs[0].metadata["delivery_id"] == "delivery-1"


@pytest.mark.asyncio
async def test_webhook_channel_uses_configured_sink_for_async_output() -> None:
    sink = _OutboundSink()
    channel = SdkWebhookChannel(outbound_sink=sink)
    conversation = channel._conversation_ref("custom-a", "room/1")

    receipt = await channel.send(
        SdkOutboundMessage(
            delivery_id="delivery-2",
            conversation_ref=conversation,
            content=(TextContent("Later"),),
            created_at=datetime.now(UTC),
        )
    )

    assert receipt.status.value == "accepted_by_platform"
    assert len(sink.messages) == 1
    assert sink.messages[0].channel_id == "custom-a"
    assert sink.messages[0].conversation_id == "room/1"
    assert sink.messages[0].text == "Later"

from __future__ import annotations

import pytest

from imcodex.models import InboundMessage, OutboundMessage


class StubService:
    def __init__(self, outbound: list[OutboundMessage] | None = None, error: Exception | None = None) -> None:
        self.outbound = outbound or []
        self.error = error
        self.seen: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        self.seen.append(message)
        if self.error is not None:
            raise self.error
        return list(self.outbound)


class CapturingAdapter:
    channel_id = "qq"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send_message(self, message: OutboundMessage) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_channel_middleware_dispatches_to_service_and_sets_reply_metadata() -> None:
    from imcodex.channels.middleware import GENERIC_USER_ERROR_TEXT, UnifiedChannelMiddleware

    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)

    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="group:group-1",
        user_id="user-1",
        message_id="msg-1",
        text="inspect repo",
    )

    await middleware.handle_inbound(adapter, inbound, reply_to_message_id="msg-1")

    assert service.seen == [inbound]
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Done"
    assert adapter.sent[0].metadata["reply_to_message_id"] == "msg-1"
    assert adapter.sent[0].text != GENERIC_USER_ERROR_TEXT


@pytest.mark.asyncio
async def test_channel_middleware_hides_raw_exception_details_from_user() -> None:
    from imcodex.channels.middleware import GENERIC_USER_ERROR_TEXT, UnifiedChannelMiddleware

    service = StubService(error=RuntimeError("<html>" + ("x" * 500)))
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)

    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="hello",
    )

    await middleware.handle_inbound(adapter, inbound, reply_to_message_id="msg-1")

    assert len(adapter.sent) == 1
    assert adapter.sent[0].message_type == "error"
    assert adapter.sent[0].text == GENERIC_USER_ERROR_TEXT
    assert "<html>" not in adapter.sent[0].text
    assert adapter.sent[0].metadata["reply_to_message_id"] == "msg-1"

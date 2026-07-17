from __future__ import annotations

import json

import httpx
import pytest

from imcodex.channels import MultiplexOutboundSink, WebhookOutboundSink
from imcodex.models import OutboundMessage


@pytest.mark.asyncio
async def test_webhook_outbound_sink_sends_contract_and_surfaces_http_failure() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
        )
        with pytest.raises(httpx.HTTPStatusError):
            await sink.send_message(
                OutboundMessage(
                    channel_id="gateway",
                    conversation_id="conv-1",
                    message_type="turn_result",
                    text="done",
                    request_id="req-1",
                    metadata={"trace_id": "trace-1"},
                )
            )

    assert json.loads(requests[0].content) == {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "message_type": "turn_result",
        "text": "done",
        "request_id": "req-1",
        "metadata": {"trace_id": "trace-1"},
    }
    assert requests[0].headers["Authorization"] == "Bearer outbound-secret"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_webhook_outbound_sink_retries_with_stable_delivery_id() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(204)

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            sleep=capture_sleep,
        )
        await sink.send_message(
            OutboundMessage(
                channel_id="gateway",
                conversation_id="conv-1",
                message_type="turn_result",
                text="done",
                metadata={"delivery_id": "imcodex:native:stable"},
            )
        )

    assert len(requests) == 2
    assert [json.loads(request.content) for request in requests] == [
        json.loads(requests[0].content),
        json.loads(requests[0].content),
    ]
    assert delays == [1]


def test_webhook_outbound_sink_bounds_untrusted_retry_after() -> None:
    request = httpx.Request("POST", "https://gateway.example/outbound")

    assert (
        WebhookOutboundSink._retry_after(
            httpx.Response(429, request=request, headers={"Retry-After": "999999999"}),
            1,
        )
        == 5
    )
    assert (
        WebhookOutboundSink._retry_after(
            httpx.Response(429, request=request, headers={"Retry-After": "inf"}),
            2,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_multiplex_outbound_sink_prefers_exact_channel_adapter() -> None:
    class Sink:
        def __init__(self) -> None:
            self.messages: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.messages.append(message)

    telegram = Sink()
    fallback = Sink()
    sink = MultiplexOutboundSink(
        channel_sinks={"telegram": telegram},
        default_sink=fallback,
    )
    assert sink.can_deliver("telegram") is True
    assert sink.can_deliver("gateway") is True
    telegram_message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat:42",
        message_type="turn_result",
        text="telegram",
    )
    gateway_message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="turn_result",
        text="gateway",
    )

    await sink.send_message(telegram_message)
    await sink.send_message(gateway_message)

    assert telegram.messages == [telegram_message]
    assert fallback.messages == [gateway_message]


@pytest.mark.asyncio
async def test_multiplex_never_routes_disabled_builtin_channel_to_fallback() -> None:
    class Sink:
        def __init__(self) -> None:
            self.messages: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.messages.append(message)

    fallback = Sink()
    sink = MultiplexOutboundSink(default_sink=fallback)
    assert sink.can_deliver("telegram") is False
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat:42",
        message_type="turn_result",
        text="must not leak",
    )

    with pytest.raises(RuntimeError, match="refusing fallback delivery"):
        await sink.send_message(message)

    assert fallback.messages == []


@pytest.mark.asyncio
async def test_multiplex_rejects_delivery_without_any_matching_sink() -> None:
    sink = MultiplexOutboundSink()
    assert sink.can_deliver("gateway") is False
    message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="turn_result",
        text="must not disappear",
    )

    with pytest.raises(RuntimeError, match="No outbound sink"):
        await sink.send_message(message)


def test_remote_outbound_webhook_requires_https_and_bearer_token() -> None:
    with pytest.raises(ValueError, match="requires HTTPS"):
        WebhookOutboundSink(
            "http://gateway.example/outbound",
            bearer_token="secret",
        )
    with pytest.raises(ValueError, match="OUTBOUND_WEBHOOK_TOKEN"):
        WebhookOutboundSink("https://gateway.example/outbound")

    sink = WebhookOutboundSink("http://127.0.0.1:9000/outbound")
    assert sink.bearer_token == ""

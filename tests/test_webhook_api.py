from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from imagent.contracts import OutboundMessage as SdkOutboundMessage
from imagent.contracts import TextContent

from imcodex.channels.api import create_app
from imcodex.channels.sdk_webhook import ImcodexRuntimeService, SdkWebhookChannel
from imcodex.models import InboundMessage


class _ProductPolicy:
    store = object()
    backend = object()

    def preflight_inbound_attachments(self, _message):
        return None


def _started_service(*, on_admission=None):
    channel = SdkWebhookChannel()

    async def on_message(message) -> None:
        await channel.send(
            SdkOutboundMessage(
                delivery_id=f"reply:{message.message_id}",
                conversation_ref=message.conversation_ref,
                content=(TextContent("Done"),),
                created_at=datetime.now(UTC),
                reply_to=message.message_id,
            )
        )

    async def on_operation(_operation) -> None:
        return None

    asyncio.run(channel.start(on_message, on_operation, on_admission))
    return channel, ImcodexRuntimeService(
        channel=channel,
        product_service=_ProductPolicy(),
    )


def _payload(**overrides):
    payload = {
        "channel_id": "custom",
        "conversation_id": "room-1",
        "user_id": "user-1",
        "message_id": "message-1",
        "text": "hello",
    }
    payload.update(overrides)
    return payload


def test_webhook_authenticates_before_dispatch() -> None:
    _channel, service = _started_service()
    client = TestClient(create_app(service, inbound_token="secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer wrong"},
        json=_payload(),
    )

    assert response.status_code == 401


def test_webhook_round_trips_an_immediate_sdk_reply() -> None:
    _channel, service = _started_service()
    client = TestClient(create_app(service, inbound_token="secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer secret"},
        json=_payload(),
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["text"] == "Done"


def test_webhook_rejects_a_builtin_channel_namespace() -> None:
    _channel, service = _started_service()
    client = TestClient(create_app(service, inbound_token="secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer secret"},
        json=_payload(channel_id="qq"),
    )

    assert response.status_code == 409


def test_webhook_uses_pre_media_sdk_admission() -> None:
    deliveries = 0

    class _Lease:
        async def deliver(self, message) -> None:
            nonlocal deliveries
            deliveries += 1
            await channel._on_message(message)

        async def release(self) -> None:
            raise AssertionError("completed preparation must transfer the lease")

    claims = 0

    async def on_admission(_conversation, _message_id):
        nonlocal claims
        claims += 1
        return _Lease() if claims == 1 else None

    channel, service = _started_service(on_admission=on_admission)
    client = TestClient(create_app(service, inbound_token="secret"))

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer secret"},
        json=_payload(),
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer secret"},
        json=_payload(),
    )

    assert first.json()["messages"][0]["text"] == "Done"
    assert second.json() == {"messages": []}
    assert claims == 2
    assert deliveries == 1


def test_webhook_stream_bounds_an_oversized_json_body() -> None:
    _channel, service = _started_service()
    client = TestClient(create_app(service, inbound_token="secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer secret"},
        json=_payload(text="x" * (64 * 1024)),
    )

    assert response.status_code == 413


def test_concurrent_duplicate_cannot_steal_immediate_reply_slot() -> None:
    channel = SdkWebhookChannel()
    first_preparing = asyncio.Event()
    allow_first = asyncio.Event()
    claims = 0

    async def on_message(message) -> None:
        await channel.send(
            SdkOutboundMessage(
                delivery_id="reply-1",
                conversation_ref=message.conversation_ref,
                content=(TextContent("Done"),),
                created_at=datetime.now(UTC),
                reply_to=message.message_id,
            )
        )

    class Lease:
        async def deliver(self, message) -> None:
            await on_message(message)

        async def release(self) -> None:
            return None

    async def on_admission(_conversation, _message_id):
        nonlocal claims
        claims += 1
        return Lease() if claims == 1 else None

    async def scenario() -> tuple[list, list]:
        await channel.start(on_message, lambda _operation: None, on_admission)
        inbound = InboundMessage("custom", "room-1", "user-1", "message-1", "hello")

        async def prepare(message):
            first_preparing.set()
            await allow_first.wait()
            return message

        first = asyncio.create_task(channel.receive(inbound, prepare_inbound=prepare))
        await first_preparing.wait()
        duplicate = await channel.receive(inbound)
        allow_first.set()
        return await first, duplicate

    first, duplicate = asyncio.run(scenario())

    assert [message.text for message in first] == ["Done"]
    assert duplicate == []

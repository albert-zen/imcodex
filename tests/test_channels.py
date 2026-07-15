from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile

import pytest
import httpx
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import FormData

from imcodex.channels import ChannelAccessPolicy, QQChannelAdapter, create_app
from imcodex.channels.middleware import UnifiedChannelMiddleware
from imcodex.channels.qq import OP_DISPATCH, OP_HELLO, RECONNECT_MAX_DELAY_S
from imcodex.channels.api import (
    MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS,
    _InboundWebhookGuard,
)
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(stream, format="PNG")
    return stream.getvalue()


PNG = _png()


class StubService:
    async def handle_inbound(self, message):
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


class CountingService:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store
        self.calls: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage):
        self.calls.append(message)
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def test_webhook_inbound_returns_messages() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["message_type"] == "accepted"


def test_webhook_multipart_image_uses_shared_staging_pipeline(tmp_path: Path) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    media_dir = tmp_path / "inbound-media"
    client = TestClient(
        create_app(
            service,
            inbound_token="webhook-secret",
            media_dir=media_dir,
        )
    )

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "image-1",
        },
        files={"images": ("sender-name.exe", PNG, "application/octet-stream")},
    )

    assert response.status_code == 200
    assert len(service.calls) == 1
    inbound = service.calls[0]
    assert inbound.text == ""
    assert inbound.input_error is None
    assert len(inbound.attachments) == 1
    attachment = inbound.attachments[0]
    assert attachment.kind == "image"
    assert attachment.content_type == "image/png"
    assert attachment.size_bytes == len(PNG)
    path = Path(attachment.local_path)
    assert path.parent == media_dir.absolute()
    assert path.suffix == ".png"
    assert path.read_bytes() == PNG


def test_webhook_multipart_rejects_internal_path_fields(tmp_path: Path) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(
        create_app(
            service,
            inbound_token="webhook-secret",
            media_dir=tmp_path / "media",
        )
    )

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "image-1",
            "local_path": "/etc/passwd",
        },
        files={"images": ("image.png", PNG, "image/png")},
    )

    assert response.status_code == 422
    assert service.calls == []
    assert not (tmp_path / "media").exists()


def test_webhook_duplicate_is_dropped_before_second_image_staging(tmp_path: Path) -> None:
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = CountingService(store)
    media_dir = tmp_path / "media"
    client = TestClient(
        create_app(
            service,
            inbound_token="webhook-secret",
            media_dir=media_dir,
        )
    )
    data = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "image-1",
    }

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data=data,
        files={"images": ("image.png", PNG, "image/png")},
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data=data,
        files={"images": ("different.png", b"not-an-image", "image/png")},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(service.calls) == 1
    assert len(list(media_dir.iterdir())) == 1


def test_webhook_multipart_too_many_images_is_a_stable_input_error(tmp_path: Path) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(
        create_app(
            service,
            inbound_token="webhook-secret",
            media_dir=tmp_path / "media",
        )
    )

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "image-1",
        },
        files=[("images", (f"{index}.png", PNG, "image/png")) for index in range(5)],
    )

    assert response.status_code == 200
    assert service.calls[0].input_error == "too_many_images"
    assert service.calls[0].attachments == ()
    assert not (tmp_path / "media").exists()


def test_webhook_multipart_rejects_an_oversized_file_during_parsing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("imcodex.channels.api.MAX_IMAGE_BYTES", 32)
    service = CountingService(ConversationStore(clock=lambda: 1.0))
    media_dir = tmp_path / "media"
    client = TestClient(
        create_app(
            service,
            inbound_token="webhook-secret",
            media_dir=media_dir,
        )
    )

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "image-1",
        },
        files={"images": ("image.png", b"x" * 33, "image/png")},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Inbound image file is too large."}
    assert service.calls == []
    assert not media_dir.exists()


def test_webhook_inbound_rejects_invalid_bearer_token() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer wrong"},
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 401


def test_webhook_inbound_without_token_is_loopback_only() -> None:
    app = create_app(StubService())
    remote_client = TestClient(app, client=("198.51.100.10", 50000))
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    body = {
        "channel_id": "demo",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "hello",
    }

    assert remote_client.post("/api/channels/webhook/inbound", json=body).status_code == 403
    assert loopback_client.post("/api/channels/webhook/inbound", json=body).status_code == 200


def test_loopback_multipart_requires_explicit_non_simple_header(tmp_path: Path) -> None:
    service = CountingService(ConversationStore(clock=lambda: 1.0))
    client = TestClient(
        create_app(service, media_dir=tmp_path / "media"),
        client=("127.0.0.1", 50000),
    )
    data = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "image-1",
    }

    missing_header = client.post(
        "/api/channels/webhook/inbound",
        data=data,
        files={"images": ("image.png", PNG, "image/png")},
    )
    hostile = client.post(
        "/api/channels/webhook/inbound",
        headers={
            "Host": "attacker.example",
            "Origin": "https://attacker.example",
            "X-IMCodex-Webhook": "1",
        },
        data=data,
        files={"images": ("image.png", PNG, "image/png")},
    )
    local_adapter = client.post(
        "/api/channels/webhook/inbound",
        headers={"X-IMCodex-Webhook": "1"},
        data=data,
        files={"images": ("image.png", PNG, "image/png")},
    )

    expected_denial = {
        "detail": "Loopback multipart requests require X-IMCodex-Webhook: 1."
    }
    assert missing_header.status_code == 403
    assert missing_header.json() == expected_denial
    assert hostile.status_code == 403
    assert hostile.json() == {
        "detail": "Browser-origin webhook requests require IMCODEX_INBOUND_WEBHOOK_TOKEN."
    }
    assert local_adapter.status_code == 200
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_webhook_releases_multipart_capacity_before_downstream_handling(
    tmp_path: Path,
) -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.calls = 0
            self.at_capacity = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_inbound(self, message):
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 3:
                self.at_capacity.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1
            return [
                OutboundMessage(
                    channel_id=message.channel_id,
                    conversation_id=message.conversation_id,
                    message_type="accepted",
                    text="Accepted",
                )
            ]

    service = BlockingService()
    app = create_app(
        service,
        inbound_token="webhook-secret",
        media_dir=tmp_path / "media",
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer webhook-secret"}

    async def submit(index: int):
        fields = [
            ("channel_id", (None, "gateway")),
            ("conversation_id", (None, f"conv-{index}")),
            ("user_id", (None, "u1")),
            ("message_id", (None, f"message-{index}")),
            ("text", (None, "hello")),
            ("images", (f"image-{index}.png", PNG, "image/png")),
        ]
        return await client.post(
            "/api/channels/webhook/inbound",
            headers=headers,
            files=fields,
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tasks = [asyncio.create_task(submit(index)) for index in range(3)]
        await asyncio.wait_for(service.at_capacity.wait(), timeout=5.0)
        await asyncio.sleep(0)
        assert service.calls == 3
        service.release.set()
        responses = await asyncio.gather(*tasks)

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert service.calls == 3
    assert service.max_active == 3


@pytest.mark.asyncio
async def test_webhook_releases_multipart_capacity_after_preflight_before_sink(
    tmp_path: Path,
) -> None:
    class BlockingSink:
        def __init__(self) -> None:
            self.active = 0
            self.at_capacity = asyncio.Event()
            self.release = asyncio.Event()

        async def send_message(self, _message: OutboundMessage) -> None:
            self.active += 1
            if self.active == 3:
                self.at_capacity.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    class PreflightService:
        def __init__(self) -> None:
            self.store = ConversationStore(clock=lambda: 1.0)
            self.outbound_sink = BlockingSink()

        def preflight_inbound_attachments(
            self,
            inbound: InboundMessage,
        ) -> list[OutboundMessage]:
            return [
                OutboundMessage(
                    channel_id=inbound.channel_id,
                    conversation_id=inbound.conversation_id,
                    message_type="error",
                    text="Local image paths are unavailable.",
                )
            ]

        async def handle_inbound(self, _message: InboundMessage):
            raise AssertionError("attachment preflight must skip the service")

    service = PreflightService()
    media_dir = tmp_path / "media"
    app = create_app(
        service,
        inbound_token="webhook-secret",
        media_dir=media_dir,
    )
    transport = httpx.ASGITransport(app=app)

    async def submit(index: int):
        fields = [
            ("channel_id", (None, "gateway")),
            ("conversation_id", (None, f"conv-{index}")),
            ("user_id", (None, "u1")),
            ("message_id", (None, f"preflight-{index}")),
            ("images", (f"image-{index}.png", PNG, "image/png")),
        ]
        return await client.post(
            "/api/channels/webhook/inbound",
            headers={"Authorization": "Bearer webhook-secret"},
            files=fields,
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tasks = [asyncio.create_task(submit(index)) for index in range(3)]
        await asyncio.wait_for(service.outbound_sink.at_capacity.wait(), timeout=5)
        assert service.outbound_sink.active == 3
        service.outbound_sink.release.set()
        responses = await asyncio.gather(*tasks)

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert not media_dir.exists()


@pytest.mark.asyncio
async def test_webhook_multipart_ingress_timeout_includes_capacity_wait(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S",
        0.02,
    )
    app = create_app(StubService(), inbound_token="webhook-secret")
    semaphore = app.state.webhook_multipart_semaphore
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        await semaphore.acquire()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer webhook-secret"},
                files={"channel_id": (None, "gateway")},
            )
    finally:
        for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
            semaphore.release()

    assert response.status_code == 408
    assert response.json() == {"detail": "Inbound multipart upload timed out."}


@pytest.mark.asyncio
async def test_webhook_multipart_retention_timeout_includes_conversation_wait(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S",
        0.05,
    )

    class BlockingService(StubService):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_inbound(self, message: InboundMessage):
            if message.message_id == "blocking-message":
                self.started.set()
                await self.release.wait()
            return await super().handle_inbound(message)

    service = BlockingService()
    app = create_app(
        service,
        inbound_token="webhook-secret",
        media_dir=tmp_path / "media",
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer webhook-secret"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blocking = asyncio.create_task(
            client.post(
                "/api/channels/webhook/inbound",
                headers=headers,
                json={
                    "channel_id": "gateway",
                    "conversation_id": "same-conversation",
                    "user_id": "u1",
                    "message_id": "blocking-message",
                    "text": "hold",
                },
            )
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)
        response = await client.post(
            "/api/channels/webhook/inbound",
            headers=headers,
            data={
                "channel_id": "gateway",
                "conversation_id": "same-conversation",
                "user_id": "u1",
                "message_id": "waiting-image",
            },
            files={"images": ("image.png", PNG, "image/png")},
        )

        semaphore = app.state.webhook_multipart_semaphore
        for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
            await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
            semaphore.release()
        service.release.set()
        blocking_response = await asyncio.wait_for(blocking, timeout=1)

    assert response.status_code == 408
    assert response.json() == {"detail": "Inbound multipart upload timed out."}
    assert blocking_response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_timeout_does_not_wait_forever_for_form_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S",
        0.05,
    )
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_FORM_CLOSE_GRACE_S",
        0.01,
    )
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    original_close = FormData.close

    async def blocking_close(self: FormData) -> None:
        close_started.set()
        await release_close.wait()
        await original_close(self)

    monkeypatch.setattr(FormData, "close", blocking_close)

    class PreflightService:
        def __init__(self) -> None:
            self.store = ConversationStore(clock=lambda: 1.0)

        def preflight_inbound_attachments(
            self,
            inbound: InboundMessage,
        ) -> list[OutboundMessage]:
            return [
                OutboundMessage(
                    channel_id=inbound.channel_id,
                    conversation_id=inbound.conversation_id,
                    message_type="error",
                    text="Local image paths are unavailable.",
                )
            ]

        async def handle_inbound(self, _message: InboundMessage):
            raise AssertionError("attachment preflight must skip the service")

    app = create_app(
        PreflightService(),
        inbound_token="webhook-secret",
        media_dir=tmp_path / "media",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer webhook-secret"},
                data={
                    "channel_id": "gateway",
                    "conversation_id": "conv-close",
                    "user_id": "u1",
                    "message_id": "image-close",
                },
                files={"images": ("image.png", PNG, "image/png")},
            ),
            timeout=1,
        )

    assert response.status_code == 408
    assert response.json() == {"detail": "Inbound multipart upload timed out."}
    assert close_started.is_set()
    cleanup_tasks = tuple(app.state.webhook_form_cleanup_tasks)
    assert cleanup_tasks
    semaphore = app.state.webhook_multipart_semaphore
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        semaphore.release()

    release_close.set()
    await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=1)
    await asyncio.sleep(0)
    assert app.state.webhook_form_cleanup_tasks == set()


@pytest.mark.asyncio
async def test_webhook_validation_error_keeps_bounded_form_close_ownership(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S",
        0.05,
    )
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_FORM_CLOSE_GRACE_S",
        0.01,
    )
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    original_close = FormData.close

    async def blocking_close(self: FormData) -> None:
        close_started.set()
        await release_close.wait()
        await original_close(self)

    monkeypatch.setattr(FormData, "close", blocking_close)
    app = create_app(
        StubService(),
        inbound_token="webhook-secret",
        media_dir=tmp_path / "media",
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post(
                "/api/channels/webhook/inbound",
                headers={"Authorization": "Bearer webhook-secret"},
                data={
                    "channel_id": "gateway",
                    "conversation_id": "conv-invalid-close",
                    "user_id": "u1",
                    "message_id": "invalid-close",
                    "local_path": "/not/accepted",
                },
                files={"images": ("image.png", PNG, "image/png")},
            ),
            timeout=1,
        )

    assert response.status_code == 408
    assert response.json() == {"detail": "Inbound multipart upload timed out."}
    assert close_started.is_set()
    cleanup_tasks = tuple(app.state.webhook_form_cleanup_tasks)
    assert cleanup_tasks
    semaphore = app.state.webhook_multipart_semaphore
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        semaphore.release()

    release_close.set()
    await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=1)
    await asyncio.sleep(0)
    assert app.state.webhook_form_cleanup_tasks == set()


@pytest.mark.asyncio
async def test_webhook_form_close_error_does_not_mask_validation_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    close_calls = 0

    async def failed_close(_form: FormData) -> None:
        nonlocal close_calls
        close_calls += 1
        raise OSError("synthetic close failure")

    monkeypatch.setattr(FormData, "close", failed_close)
    app = create_app(
        StubService(),
        inbound_token="webhook-secret",
        media_dir=tmp_path / "media",
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/channels/webhook/inbound",
            headers={"Authorization": "Bearer webhook-secret"},
            data={
                "channel_id": "gateway",
                "conversation_id": "conv-invalid-close-error",
                "user_id": "u1",
                "message_id": "invalid-close-error",
                "local_path": "/not/accepted",
            },
            files={"images": ("image.png", PNG, "image/png")},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Inbound multipart body contains unsupported fields."
    }
    assert close_calls == 1
    await asyncio.sleep(0)
    assert app.state.webhook_form_cleanup_tasks == set()
    semaphore = app.state.webhook_multipart_semaphore
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        semaphore.release()


@pytest.mark.asyncio
async def test_webhook_multipart_ingress_timeout_cancels_parser_and_releases_slot(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.api.MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S",
        0.02,
    )
    created_files = []

    def tracked_spooled_file(*args, **kwargs):
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        created_files.append(file)
        return file

    monkeypatch.setattr(
        "starlette.formparsers.SpooledTemporaryFile",
        tracked_spooled_file,
    )
    monkeypatch.setattr(
        "starlette.formparsers.MultiPartParser.spool_max_size",
        1,
    )
    boundary = "imcodex-timeout-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="images"; filename="image.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + PNG[:16]

    async def slow_body():
        yield prefix
        await asyncio.Event().wait()

    app = create_app(StubService(), inbound_token="webhook-secret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/channels/webhook/inbound",
            headers={
                "Authorization": "Bearer webhook-secret",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=slow_body(),
        )

    assert response.status_code == 408
    assert response.json() == {"detail": "Inbound multipart upload timed out."}
    assert created_files
    assert all(getattr(file, "_rolled", False) for file in created_files)
    assert all(file.closed for file in created_files)
    semaphore = app.state.webhook_multipart_semaphore
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    for _ in range(MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS):
        semaphore.release()


def test_standalone_webhook_app_starts_and_stops_materializer_once(tmp_path: Path) -> None:
    class LifecycleMaterializer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def start(self) -> None:
            self.calls.append("start")

        async def stop(self) -> None:
            self.calls.append("stop")

    materializer = LifecycleMaterializer()
    app = create_app(
        StubService(),
        media_dir=tmp_path / "media",
        media_materializer=materializer,
    )

    with TestClient(app):
        assert materializer.calls == ["start"]

    assert materializer.calls == ["start", "stop"]


def test_webhook_authenticates_before_parsing_json() -> None:
    remote = TestClient(
        create_app(StubService()),
        client=("198.51.100.10", 50000),
    )
    protected = TestClient(
        create_app(StubService(), inbound_token="webhook-secret"),
        client=("198.51.100.10", 50000),
    )

    assert (
        remote.post(
            "/api/channels/webhook/inbound",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 403
    )
    assert (
        protected.post(
            "/api/channels/webhook/inbound",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 401
    )


def test_webhook_rejects_non_ascii_authorization_bytes_without_crashing() -> None:
    guard = _InboundWebhookGuard(object(), configured_token="webhook-secret")

    assert guard._authorization_denial(
        scope={"client": ("198.51.100.10", 50000)},
        authorization=b"Bearer \xff",
    ) == (401, "Invalid inbound webhook credentials.")


def test_webhook_rejects_oversized_body_before_model_parsing() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "x" * (64 * 1024),
        },
    )

    assert response.status_code == 413


def test_webhook_stream_bounds_chunked_body_without_content_length() -> None:
    client = TestClient(
        create_app(StubService(), inbound_token="webhook-secret"),
        raise_server_exceptions=False,
    )

    def chunks():
        yield b"{" + b"x" * (64 * 1024)
        yield b"}"

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={
            "Authorization": "Bearer webhook-secret",
            "Content-Type": "application/json",
        },
        content=chunks(),
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413


def test_webhook_cannot_claim_a_built_in_channel_route() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "telegram",
            "conversation_id": "chat:123456",
            "user_id": "attacker",
            "message_id": "m1",
            "text": "bind this route",
        },
    )

    assert response.status_code == 409
    assert service.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {
            "attachments": [
                {
                    "kind": "image",
                    "content_type": "image/png",
                    "local_path": "/etc/passwd",
                    "size_bytes": 1,
                }
            ]
        },
        {"input_error": "image_download_failed"},
    ],
)
def test_webhook_cannot_inject_internal_attachment_fields(extra: dict) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "inspect",
        **extra,
    }

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 422
    assert service.calls == []


def test_webhook_uses_persisted_message_id_deduplication(tmp_path) -> None:
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(service.calls) == 1
    assert second.json()["messages"][0]["text"] == "Accepted"


def test_webhook_delivers_immediate_messages_to_configured_outbound_sink() -> None:
    class Sink:
        def __init__(self) -> None:
            self.messages: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.messages.append(message)

    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    sink = Sink()
    service.outbound_sink = sink
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 200
    assert [message.text for message in sink.messages] == ["Accepted"]
    assert response.json()["messages"][0]["text"] == "Accepted"


def test_webhook_multipart_does_not_relabel_downstream_timeout_as_ingress() -> None:
    class TimeoutSink:
        async def send_message(self, _message: OutboundMessage) -> None:
            raise TimeoutError("downstream gateway timed out")

    service = CountingService(ConversationStore(clock=lambda: 1.0))
    service.outbound_sink = TimeoutSink()
    client = TestClient(
        create_app(service, inbound_token="webhook-secret"),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        data={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "image-timeout",
        },
        files={"images": ("image.png", PNG, "image/png")},
    )

    assert response.status_code == 500


def test_webhook_retries_cached_delivery_without_reexecuting_command(tmp_path) -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.attempts = 0
            self.delivered: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.HTTPStatusError(
                    "503",
                    request=httpx.Request("POST", "https://gateway.example/outbound"),
                    response=httpx.Response(503),
                )
            self.delivered.append(message)

    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = CountingService(store)
    sink = FlakySink()
    service.outbound_sink = sink
    client = TestClient(
        create_app(service, inbound_token="webhook-secret"),
        raise_server_exceptions=False,
    )
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/new",
    }

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert first.status_code == 500
    assert second.status_code == 200
    assert len(service.calls) == 1
    assert sink.attempts == 2
    assert [message.text for message in sink.delivered] == ["Accepted"]
    assert sink.delivered[0].metadata["delivery_id"].startswith("imcodex:")
    assert second.json()["messages"][0]["text"] == "Accepted"


@pytest.mark.parametrize(
    "field_name",
    ["channel_id", "conversation_id", "user_id", "message_id"],
)
def test_webhook_requires_non_empty_stable_routing_ids(field_name: str) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }
    body[field_name] = ""

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 422
    assert service.calls == []


def test_qq_adapter_normalizes_group_mention_message() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    inbound = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "<@123>  inspect repo",
            "group_openid": "group-1",
            "author": {"member_openid": "user-1"},
        },
    )

    assert inbound is not None
    assert inbound.conversation_id == "group:group-1"
    assert inbound.text == "inspect repo"


@pytest.mark.asyncio
async def test_qq_adapter_sends_markdown_messages_by_default() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="**Accepted**",
                metadata={"reply_to_message_id": "msg-1"},
            )
        )

    message_body = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/v2/groups/group-1/messages"
    assert message_body == {
        "markdown": {"content": "**Accepted**"},
        "msg_type": 2,
        "msg_seq": 1,
        "msg_id": "msg-1",
    }


@pytest.mark.asyncio
async def test_qq_adapter_sends_plain_text_messages_when_disabled() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=False,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="**Accepted**",
            )
        )

    message_body = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/v2/users/user-1/messages"
    assert message_body == {
        "content": "**Accepted**",
        "msg_type": 0,
        "msg_seq": 1,
    }


@pytest.mark.parametrize("status_code", [400, 403])
@pytest.mark.asyncio
async def test_qq_adapter_retries_plain_text_when_markdown_send_fails(
    status_code: int,
) -> None:
    message_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        message_requests.append(request)
        if len(message_requests) == 1:
            return httpx.Response(status_code, json={"message": "markdown unsupported"})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=True,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="**Accepted**",
                metadata={"reply_to_message_id": "msg-1"},
            )
        )

    assert [json.loads(request.content) for request in message_requests] == [
        {
            "markdown": {"content": "**Accepted**"},
            "msg_type": 2,
            "msg_seq": 1,
            "msg_id": "msg-1",
        },
        {
            "content": "**Accepted**",
            "msg_type": 0,
            "msg_seq": 1,
            "msg_id": "msg-1",
        },
    ]


@pytest.mark.asyncio
async def test_qq_adapter_does_not_retry_plain_text_for_server_errors() -> None:
    message_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        message_requests.append(request)
        return httpx.Response(500, json={"message": "temporary failure"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=True,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        with pytest.raises(httpx.HTTPStatusError):
            await adapter.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id="group:group-1",
                    message_type="turn_result",
                    text="**Accepted**",
                )
            )

    assert len(message_requests) == 1


@pytest.mark.asyncio
async def test_qq_adapter_delegates_standardized_inbound_message_to_middleware() -> None:
    class CapturingMiddleware:
        def __init__(self) -> None:
            self.seen: list[InboundMessage] = []

        async def handle_inbound(self, adapter, inbound, *, reply_to_message_id=None):
            self.seen.append(inbound)
            await adapter.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id=inbound.conversation_id,
                    message_type="turn_result",
                    text="Accepted",
                    metadata={"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {},
                )
            )

    middleware = CapturingMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    sent: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        sent.append(message)

    adapter.send_message = capture  # type: ignore[method-assign]

    await adapter.handle_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "hello",
            "author": {"user_openid": "user-1"},
        },
    )

    assert sent
    assert middleware.seen
    assert middleware.seen[0].text == "hello"
    assert sent[0].message_type == "turn_result"
    assert sent[0].metadata["reply_to_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_qq_adapter_emits_ready_event_and_health_update(monkeypatch) -> None:
    observed_events: list[dict] = []
    observed_health: list[tuple[str, dict]] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.emit_event", capture_event)
    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self._messages = iter(
                [
                    json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1}}),
                    json.dumps(
                        {
                            "op": OP_DISPATCH,
                            "t": "READY",
                            "d": {"session_id": "session-1"},
                        }
                    ),
                ]
            )

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fast_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        websocket_factory=lambda _url: FakeConnection(FakeWebSocket()),
        sleep=fast_sleep,
    )

    await adapter._run_session("ws://gateway", "token")

    assert [event["event"] for event in observed_events] == ["qq.gateway.ready"]
    assert observed_health == [("qq", {"connected": True, "session_id": "session-1", "status": "connected"})]


@pytest.mark.asyncio
async def test_qq_socket_reader_queues_messages_without_waiting_for_codex() -> None:
    class BlockingMiddleware:
        def __init__(self) -> None:
            self.seen: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None):
            self.seen.append(inbound.message_id)
            if inbound.message_id == "m1":
                self.first_started.set()
                await self.release_first.wait()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.consumed = 0
            self.sent: list[dict] = []
            self.messages = [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 60_000}},
                {"op": OP_DISPATCH, "s": 1, "t": "READY", "d": {"session_id": "session-1"}},
                {
                    "op": OP_DISPATCH,
                    "s": 2,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {"id": "m1", "content": "first", "author": {"user_openid": "u1"}},
                },
                {
                    "op": OP_DISPATCH,
                    "s": 3,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {"id": "m2", "content": "second", "author": {"user_openid": "u1"}},
                },
            ]

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.consumed >= len(self.messages):
                raise StopAsyncIteration
            message = self.messages[self.consumed]
            self.consumed += 1
            return json.dumps(message)

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    middleware = BlockingMiddleware()
    websocket = FakeWebSocket()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        websocket_factory=lambda _url: FakeConnection(websocket),
        access_policy=ChannelAccessPolicy.allow_all(),
    )

    session = asyncio.create_task(adapter._run_session("ws://gateway", "token"))
    await middleware.first_started.wait()
    await asyncio.wait_for(session, timeout=0.2)

    assert websocket.consumed == len(websocket.messages)
    assert middleware.seen == ["m1"]
    assert adapter._last_seq == 1

    middleware.release_first.set()
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert middleware.seen == ["m1", "m2"]
    assert adapter._last_seq == 3
    await adapter.stop()


@pytest.mark.asyncio
async def test_qq_inbound_worker_retries_before_advancing_resume_sequence() -> None:
    class FlakyMiddleware:
        def __init__(self) -> None:
            self.attempts = 0

        async def handle_inbound(self, _adapter, _inbound, *, reply_to_message_id=None):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary delivery failure")

    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()

    async def controlled_sleep(_delay: float) -> None:
        retry_waiting.set()
        await release_retry.wait()

    middleware = FlakyMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        sleep=controlled_sleep,
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    adapter._queue_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {"id": "m1", "content": "first", "author": {"user_openid": "u1"}},
        9,
    )

    await retry_waiting.wait()
    assert adapter._last_seq is None
    release_retry.set()
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert middleware.attempts == 2
    assert adapter._last_seq == 9
    await adapter.stop()


def test_qq_startup_configuration_normalizes_credentials() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="  app-id  ",
        client_secret="  client-secret  ",
        middleware=object(),
        api_base="  https://api.sgroup.qq.com/  ",
        http_client=object(),
    )

    adapter.validate_startup_configuration()

    assert adapter.app_id == "app-id"
    assert adapter.client_secret == "client-secret"
    assert adapter.api_base == "https://api.sgroup.qq.com"


def test_qq_startup_configuration_rejects_blank_credentials() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="   ",
        client_secret="secret",
        middleware=object(),
        http_client=object(),
    )

    with pytest.raises(RuntimeError, match="requires app_id and client_secret"):
        adapter.validate_startup_configuration()


def test_qq_startup_configuration_rejects_invalid_api_base() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        api_base="ftp://api.sgroup.qq.com",
        http_client=object(),
    )

    with pytest.raises(ValueError, match=r"IMCODEX_QQ_API_BASE must be an HTTP\(S\) URL"):
        adapter.validate_startup_configuration()


@pytest.mark.asyncio
async def test_qq_adapter_start_survives_initial_network_failure(monkeypatch) -> None:
    observed_health: list[tuple[str, dict]] = []

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FailingHttpClient:
        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://bots.qq.com/app/getAppAccessToken")
            raise httpx.ConnectError("network unavailable", request=request)

    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    delays: list[float] = []

    async def controlled_sleep(seconds: float) -> None:
        delays.append(seconds)
        sleep_started.set()
        await release_sleep.wait()

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        http_client=FailingHttpClient(),
        sleep=controlled_sleep,
    )

    await adapter.start()
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert adapter._runner_task is not None
    assert not adapter._runner_task.done()
    assert delays == [1.0]
    assert observed_health[0] == (
        "qq",
        {
            "enabled": True,
            "connected": False,
            "status": "connecting",
            "inbound_access_ready": True,
            "access_policy_mode": "platform",
            "access_match": "any",
            "allowed_user_count": 0,
            "allowed_conversation_count": 0,
        },
    )
    assert observed_health[-1] == (
        "qq",
        {
            "connected": False,
            "session_id": None,
            "status": "reconnecting",
            "error_type": "ConnectError",
            "retry_delay_s": 1.0,
        },
    )

    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["token", "gateway"])
async def test_qq_schema_errors_never_log_secret_response_fields(
    caplog,
    failure_stage: str,
) -> None:
    secret = "qq-response-super-secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            if failure_stage == "token":
                return httpx.Response(200, json={"accessToken": secret})
            return httpx.Response(200, json={"access_token": "valid", "expires_in": 7200})
        return httpx.Response(
            200,
            json={"websocket": f"wss://gateway.example/?ticket={secret}"},
        )

    async def stop_after_failure(_delay: float) -> None:
        adapter._stop_event.set()

    caplog.set_level("WARNING")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            http_client=client,
            sleep=stop_after_failure,
        )
        await adapter._run_forever()

    assert secret not in caplog.text


def test_qq_adapter_reconnect_delay_is_capped() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    assert adapter._reconnect_delay(0) == 1.0
    assert adapter._reconnect_delay(1) == 1.0
    assert adapter._reconnect_delay(3) == 4.0
    assert adapter._reconnect_delay(100) == RECONNECT_MAX_DELAY_S


@pytest.mark.asyncio
async def test_channel_middleware_keeps_distinct_message_ids_with_identical_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.channels.middleware.emit_event", capture_event)

    clock = iter([1.0, 1.0, 1.1, 1.1])
    store = ConversationStore(clock=lambda: next(clock))
    service = CountingService(store)
    middleware = UnifiedChannelMiddleware(service=service)

    class FakeAdapter:
        channel_id = "qq"

        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.sent.append(message)

    adapter = FakeAdapter()
    inbound_1 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m1",
        text="Codex help这种命令你觉得会很重吗？",
    )
    inbound_2 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m2",
        text="Codex help这种命令你觉得会很重吗？",
    )

    await middleware.handle_inbound(adapter, inbound_1, reply_to_message_id="m1")
    await middleware.handle_inbound(adapter, inbound_2, reply_to_message_id="m2")

    assert [message.message_type for message in adapter.sent] == [
        "accepted",
        "accepted",
    ]
    assert [message.message_id for message in service.calls] == ["m1", "m2"]
    assert [event["event"] for event in observed_events] == [
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
    ]


@pytest.mark.asyncio
async def test_channel_middleware_drops_persisted_duplicate_message_id(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    first_store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    first_service = CountingService(first_store)
    first_middleware = UnifiedChannelMiddleware(service=first_service)

    class FakeAdapter:
        channel_id = "telegram"

        async def send_message(self, _message: OutboundMessage) -> None:
            return None

    inbound = InboundMessage(
        channel_id="telegram",
        conversation_id="chat:42",
        user_id="42",
        message_id="42:7",
        text="/status",
    )
    await first_middleware.handle_inbound(FakeAdapter(), inbound)

    reloaded_store = ConversationStore(clock=lambda: 10.0, state_path=state_path)
    reloaded_service = CountingService(reloaded_store)
    reloaded_middleware = UnifiedChannelMiddleware(service=reloaded_service)
    await reloaded_middleware.handle_inbound(FakeAdapter(), inbound)

    assert reloaded_service.calls == []

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from imcodex.channels.outbound import WebhookOutboundSink
from imcodex.models import OutboundArtifact, OutboundMessage


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
                    message_type="turn/completed",
                    text="done",
                    request_id="req-1",
                    metadata={"trace_id": "trace-1"},
                )
            )

    assert json.loads(requests[0].content) == {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "message_type": "turn/completed",
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
                message_type="turn/completed",
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


@pytest.mark.asyncio
async def test_webhook_outbound_sink_sends_artifacts_as_multipart(
    tmp_path: Path,
) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    image_path = outbound_root / "preview.png"
    content = b"preview-bytes"
    image_path.write_bytes(content)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            outbound_media_dir=outbound_root,
        )
        await sink.send_message(
            OutboundMessage(
                channel_id="gateway",
                conversation_id="conv-1",
                message_type="turn/completed",
                text="Rendered preview.",
                metadata={"delivery_id": "terminal-1"},
                artifacts=[
                    OutboundArtifact(
                        kind="image",
                        local_path=str(image_path),
                        content_type="image/png",
                        filename="preview.png",
                        size_bytes=len(content),
                    )
                ],
            )
        )

    request = requests[0]
    assert request.headers["Content-Type"].startswith("multipart/form-data;")
    assert b'name="payload"' in request.content
    assert b'"kind": "image"' in request.content
    assert b'name="artifacts"; filename="preview.png"' in request.content
    assert content in request.content


@pytest.mark.asyncio
async def test_webhook_converts_missing_artifact_to_visible_notice(
    tmp_path: Path,
) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="turn/completed",
        text="Done.",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[
            OutboundArtifact(
                kind="file",
                local_path=str(outbound_root / "missing.pdf"),
                content_type="application/pdf",
                filename="missing.pdf",
                size_bytes=100,
            )
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            outbound_media_dir=outbound_root,
        )
        await sink.send_message(message)

    payload = json.loads(requests[0].content)
    assert "Attachment delivery unavailable" in payload["text"]
    assert "missing.pdf" in payload["text"]
    assert "artifacts" not in payload
    assert message.artifacts == []


@pytest.mark.asyncio
async def test_webhook_converts_permanent_multipart_rejection_to_notice(
    tmp_path: Path,
) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    artifact_path = outbound_root / "preview.png"
    artifact_path.write_bytes(b"preview")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(415)
        return httpx.Response(204)

    message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="turn/completed",
        text="Done.",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[
            OutboundArtifact(
                kind="image",
                local_path=str(artifact_path),
                content_type="image/png",
                filename="preview.png",
                size_bytes=7,
            )
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            outbound_media_dir=outbound_root,
        )
        await sink.send_message(message)

    assert len(requests) == 2
    assert requests[0].headers["Content-Type"].startswith("multipart/form-data;")
    fallback = json.loads(requests[1].content)
    assert "Attachment delivery unavailable" in fallback["text"]
    assert "preview.png" in fallback["text"]
    assert "artifacts" not in fallback
    assert message.artifacts == []


@pytest.mark.asyncio
async def test_webhook_records_each_same_named_artifact_rejected_as_one_batch(
    tmp_path: Path,
) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    first_path = outbound_root / "first.txt"
    second_path = outbound_root / "second.txt"
    first_path.write_bytes(b"one")
    second_path.write_bytes(b"two")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(415 if len(requests) == 1 else 204)

    message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="tool_delivery",
        text="Done.",
        metadata={"delivery_id": "terminal-same-name"},
        artifacts=[
            OutboundArtifact(
                kind="file",
                local_path=str(first_path),
                content_type="text/plain",
                filename="result.txt",
                size_bytes=3,
            ),
            OutboundArtifact(
                kind="file",
                local_path=str(second_path),
                content_type="text/plain",
                filename="result.txt",
                size_bytes=3,
            ),
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            outbound_media_dir=outbound_root,
        )
        await sink.send_message(message)

    receipts = message.metadata["artifact_receipts"]
    assert [receipt["status"] for receipt in receipts] == ["failed", "failed"]
    assert [receipt["local_path"] for receipt in receipts] == [
        str(first_path),
        str(second_path),
    ]
    assert message.artifacts == []


@pytest.mark.asyncio
async def test_webhook_removes_rejected_artifact_before_failed_notice_retry(
    tmp_path: Path,
) -> None:
    outbound_root = tmp_path / "outbound-media"
    outbound_root.mkdir()
    artifact_path = outbound_root / "preview.png"
    artifact_path.write_bytes(b"preview")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(415 if len(requests) == 1 else 503)

    message = OutboundMessage(
        channel_id="gateway",
        conversation_id="conv-1",
        message_type="turn/completed",
        text="Done.",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[
            OutboundArtifact(
                kind="image",
                local_path=str(artifact_path),
                content_type="image/png",
                filename="preview.png",
                size_bytes=7,
            )
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookOutboundSink(
            "https://gateway.example/outbound",
            client=client,
            bearer_token="outbound-secret",
            outbound_media_dir=outbound_root,
            max_attempts=1,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await sink.send_message(message)

    assert len(requests) == 2
    assert message.artifacts == []
    assert "Attachment delivery unavailable" in message.text


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

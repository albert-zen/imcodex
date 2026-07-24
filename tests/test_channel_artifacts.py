from __future__ import annotations

import asyncio

import pytest

from imcodex.channels.artifacts import (
    ArtifactDeliveryReceipt,
    PermanentArtifactDeliveryError,
    deliver_artifact_batch,
    stable_artifact_identity,
)
from imcodex.models import OutboundArtifact, OutboundMessage


def test_artifact_identity_survives_partial_delivery_tail_retry() -> None:
    first = OutboundArtifact(
        kind="image",
        local_path="/managed/first.png",
        content_type="image/png",
        filename="first.png",
        size_bytes=10,
        sha256="a" * 64,
    )
    second = OutboundArtifact(
        kind="file",
        local_path="/managed/report.pdf",
        content_type="application/pdf",
        filename="report.pdf",
        size_bytes=20,
        sha256="b" * 64,
    )
    message = OutboundMessage(
        channel_id="feishu",
        conversation_id="chat:1",
        message_type="turn/completed",
        text="done",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[first, second],
    )

    before = stable_artifact_identity(message, second)
    message.artifacts = [second]
    after = stable_artifact_identity(message, second)

    assert before == after
    assert before is not None


def _artifact(filename: str) -> OutboundArtifact:
    return OutboundArtifact(
        kind="file",
        local_path=f"/managed/{filename}",
        content_type="text/plain",
        filename=filename,
        size_bytes=10,
        sha256=filename,
    )


@pytest.mark.asyncio
async def test_artifact_batch_keeps_transient_failure_suffix_for_retry() -> None:
    first = _artifact("first.txt")
    second = _artifact("second.txt")
    third = _artifact("third.txt")
    message = OutboundMessage(
        channel_id="test",
        conversation_id="chat:1",
        message_type="turn/completed",
        text="done",
        artifacts=[first, second, third],
    )

    async def send_one(
        artifact: OutboundArtifact,
    ) -> ArtifactDeliveryReceipt:
        if artifact is second:
            raise RuntimeError("temporary outage")
        return ArtifactDeliveryReceipt(platform_message_id=artifact.filename)

    with pytest.raises(RuntimeError, match="temporary outage"):
        await deliver_artifact_batch(message, send_one)

    assert message.artifacts == [second, third]
    assert message.metadata["artifact_receipts"] == [
        {
            "filename": "first.txt",
            "sha256": "first.txt",
            "local_path": "/managed/first.txt",
            "status": "delivered",
            "platform_message_id": "first.txt",
            "delivery_identity": "",
        }
    ]


@pytest.mark.asyncio
async def test_artifact_batch_reports_permanent_failure_and_continues() -> None:
    rejected = _artifact("rejected.txt")
    delivered = _artifact("delivered.txt")
    message = OutboundMessage(
        channel_id="test",
        conversation_id="chat:1",
        message_type="turn/completed",
        text="done",
        artifacts=[rejected, delivered],
    )

    async def send_one(
        artifact: OutboundArtifact,
    ) -> ArtifactDeliveryReceipt:
        if artifact is rejected:
            raise PermanentArtifactDeliveryError("unsupported")
        return ArtifactDeliveryReceipt(delivery_identity="artifact-2")

    await deliver_artifact_batch(message, send_one)

    assert message.artifacts == []
    assert message.metadata["artifact_failures"] == ["rejected.txt: unsupported"]
    assert "Attachment delivery unavailable:" in message.text
    assert message.metadata["artifact_receipts"] == [
        {
            "filename": "rejected.txt",
            "sha256": "rejected.txt",
            "local_path": "/managed/rejected.txt",
            "status": "failed",
            "error": "unsupported",
            "platform_message_id": "",
            "delivery_identity": "",
        },
        {
            "filename": "delivered.txt",
            "sha256": "delivered.txt",
            "local_path": "/managed/delivered.txt",
            "status": "delivered",
            "platform_message_id": "",
            "delivery_identity": "artifact-2",
        }
    ]


@pytest.mark.asyncio
async def test_artifact_batch_checkpoints_permanent_failure_before_cancellation() -> None:
    rejected = _artifact("rejected.txt")
    cancelled = _artifact("cancelled.txt")
    message = OutboundMessage(
        channel_id="test",
        conversation_id="chat:1",
        message_type="turn/completed",
        text="done",
        artifacts=[rejected, cancelled],
    )

    async def send_one(artifact: OutboundArtifact):
        if artifact is rejected:
            raise PermanentArtifactDeliveryError("unsupported")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await deliver_artifact_batch(message, send_one)

    assert message.artifacts == [cancelled]
    assert message.metadata["artifact_failures"] == ["rejected.txt: unsupported"]
    assert "rejected.txt: unsupported" in message.text

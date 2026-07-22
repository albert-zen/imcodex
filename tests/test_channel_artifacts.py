from __future__ import annotations

from imcodex.channels.artifacts import stable_artifact_identity
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
        message_type="turn_result",
        text="done",
        metadata={"delivery_id": "terminal-1"},
        artifacts=[first, second],
    )

    before = stable_artifact_identity(message, second)
    message.artifacts = [second]
    after = stable_artifact_identity(message, second)

    assert before == after
    assert before is not None

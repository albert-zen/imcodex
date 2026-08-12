from __future__ import annotations

from pathlib import Path

import pytest

from imcodex.delivery_outbox import DeliveryOutbox, DeliveryOutboxConflict
from imcodex.models import OutboundArtifact, OutboundMessage


def _message(
    delivery_id: str = "delivery-1",
    *,
    text: str = "hello",
    artifact_path: str = "",
) -> OutboundMessage:
    artifacts = []
    if artifact_path:
        artifacts.append(
            OutboundArtifact(
                kind="file",
                local_path=artifact_path,
                content_type="text/plain",
                filename="result.txt",
                size_bytes=6,
                sha256="abc123",
            )
        )
    return OutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="tool_delivery",
        text=text,
        metadata={"delivery_id": delivery_id, "source": "channels.send"},
        artifacts=artifacts,
    )


def test_outbox_persists_pending_payload_and_artifact_reference(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    artifact = str(tmp_path / "spool" / "result.txt")
    outbox = DeliveryOutbox(path, clock=lambda: 7.0)

    staged = outbox.stage(_message(artifact_path=artifact))
    outbox.close()

    reloaded = DeliveryOutbox(path, clock=lambda: 8.0)
    pending = reloaded.list_pending()
    assert staged.pending.sequence == 1
    assert pending[0].message.text == "hello"
    assert pending[0].message.artifacts[0].local_path == artifact
    assert reloaded.referenced_artifact_paths() == {artifact}
    assert reloaded.health()["pending_count"] == 1
    reloaded.close()


def test_outbox_rejects_conflicting_payload_for_reserved_delivery_id(tmp_path: Path) -> None:
    outbox = DeliveryOutbox(tmp_path / "delivery.sqlite3")
    outbox.stage(_message())

    with pytest.raises(DeliveryOutboxConflict, match="different target or payload"):
        outbox.stage(_message(text="different"))

    outbox.close()


def test_outbox_serializes_reservation_across_process_connections(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    first = DeliveryOutbox(path)
    second = DeliveryOutbox(path)

    staged = first.stage(_message())
    replay = second.stage(_message())

    assert staged.pending.sequence == replay.pending.sequence
    with pytest.raises(DeliveryOutboxConflict):
        second.stage(_message(text="conflicting"))
    first.close()
    second.close()


def test_outbox_replays_durable_terminal_outcome_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    message = _message()
    outbox = DeliveryOutbox(path)
    outbox.stage(message)
    outbox.complete(
        "delivery-1",
        message,
        delivered=False,
        durable=False,
    )
    outbox.close()

    reloaded = DeliveryOutbox(path)
    staged = reloaded.stage(_message())
    assert staged.status == "outcome"
    assert staged.outcome.delivered is False
    assert staged.outcome.durable is False
    assert reloaded.list_pending() == []
    assert reloaded.referenced_artifact_paths() == set()
    reloaded.close()

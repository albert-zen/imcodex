from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Awaitable, Callable

from ..models import OutboundArtifact, OutboundMessage


class PermanentArtifactDeliveryError(RuntimeError):
    """An artifact cannot be delivered and retrying the same bytes will not help."""


@dataclass(frozen=True, slots=True)
class ArtifactDeliveryReceipt:
    platform_message_id: str = ""
    delivery_identity: str = ""


async def deliver_artifact_batch(
    message: OutboundMessage,
    send_one: Callable[
        [OutboundArtifact],
        Awaitable[ArtifactDeliveryReceipt | None],
    ],
) -> None:
    """Deliver artifacts with one shared retry checkpoint contract.

    A transient failure leaves the failed artifact and the unattempted suffix
    on ``message.artifacts`` for the durable outbox retry. Permanent failures
    become a text notice and do not block the remaining artifacts.
    """

    failures: list[str] = []
    original_artifacts = list(message.artifacts)
    for index, artifact in enumerate(original_artifacts):
        try:
            receipt = await send_one(artifact)
        except PermanentArtifactDeliveryError as exc:
            error = str(exc)
            failures.append(f"{artifact.filename}: {error}")
            record_artifact_failure(message, artifact, error=error)
        except asyncio.CancelledError:
            append_artifact_failures(message, failures)
            message.artifacts = original_artifacts[index:]
            raise
        except Exception:
            append_artifact_failures(message, failures)
            message.artifacts = original_artifacts[index:]
            raise
        else:
            receipt = receipt or ArtifactDeliveryReceipt()
            record_artifact_delivery(
                message,
                artifact,
                platform_message_id=receipt.platform_message_id,
                delivery_identity=receipt.delivery_identity,
            )
        message.artifacts = original_artifacts[index + 1 :]
    message.artifacts = []
    append_artifact_failures(message, failures)


async def read_managed_artifact(
    artifact: OutboundArtifact,
    *,
    root: str | Path,
) -> tuple[Path, bytes]:
    """Resolve and verify one durable artifact inside the private outbound spool."""

    try:
        managed_root = Path(root).resolve(strict=True)
        source = Path(artifact.local_path).resolve(strict=True)
        source.relative_to(managed_root)
    except (OSError, ValueError) as exc:
        raise PermanentArtifactDeliveryError(
            "artifact is outside the managed spool or no longer exists"
        ) from exc
    if not source.is_file():
        raise PermanentArtifactDeliveryError("artifact is no longer a regular file")
    try:
        content = await asyncio.to_thread(source.read_bytes)
    except OSError as exc:
        raise PermanentArtifactDeliveryError("artifact can no longer be read") from exc
    if len(content) != artifact.size_bytes:
        raise PermanentArtifactDeliveryError("artifact changed after it was staged")
    digest = await asyncio.to_thread(hashlib.sha256, content)
    if artifact.sha256 and digest.hexdigest() != artifact.sha256:
        raise PermanentArtifactDeliveryError("artifact changed after it was staged")
    return source, content


def append_artifact_failures(message: OutboundMessage, failures: list[str]) -> None:
    if not failures:
        return
    notice = "Attachment delivery unavailable:\n" + "\n".join(
        f"- {failure}" for failure in failures
    )
    if notice not in message.text:
        message.text = "\n\n".join(part for part in (message.text, notice) if part)
    recorded = message.metadata.setdefault("artifact_failures", [])
    if isinstance(recorded, list):
        for failure in failures:
            if failure not in recorded:
                recorded.append(failure)


def stable_artifact_identity(
    message: OutboundMessage,
    artifact: OutboundArtifact,
) -> str | None:
    delivery_id = str(message.metadata.get("delivery_id") or "").strip()
    if not delivery_id:
        return None
    digest = hashlib.sha256(
        (
            f"{delivery_id}\0{artifact.sha256 or artifact.local_path}"
            f"\0{artifact.size_bytes}\0{artifact.filename}"
        ).encode("utf-8")
    ).hexdigest()
    return digest


def record_artifact_delivery(
    message: OutboundMessage,
    artifact: OutboundArtifact,
    *,
    platform_message_id: str = "",
    delivery_identity: str = "",
) -> None:
    receipts = message.metadata.setdefault("artifact_receipts", [])
    if not isinstance(receipts, list):
        return
    receipts.append(
        {
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "local_path": artifact.local_path,
            "status": "delivered",
            "platform_message_id": platform_message_id,
            "delivery_identity": delivery_identity,
        }
    )


def record_artifact_failure(
    message: OutboundMessage,
    artifact: OutboundArtifact,
    *,
    error: str,
) -> None:
    receipts = message.metadata.setdefault("artifact_receipts", [])
    if not isinstance(receipts, list):
        return
    receipts.append(
        {
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "local_path": artifact.local_path,
            "status": "failed",
            "error": error,
            "platform_message_id": "",
            "delivery_identity": "",
        }
    )

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from ..models import OutboundArtifact, OutboundMessage


class PermanentArtifactDeliveryError(RuntimeError):
    """An artifact cannot be delivered and retrying the same bytes will not help."""


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

from __future__ import annotations

import asyncio
import copy

from .models import LegacyPendingDeliveryEvidence


class LegacyDeliveryEvidenceStoreMixin:
    """Read and consume pre-SDK outbox evidence during one-way migration.

    No production path can stage or update entries. The SDK migration worker
    may only remove an entry after the SDK submission reaches acceptance.
    """

    def list_legacy_delivery_evidence(
        self,
        thread_id: str | None = None,
    ) -> list[LegacyPendingDeliveryEvidence]:
        entries = self._legacy_delivery_evidence.values()
        if thread_id is not None:
            entries = (entry for entry in entries if entry.thread_id == thread_id)
        return [
            copy.deepcopy(entry)
            for entry in sorted(
                entries,
                key=lambda entry: (entry.sequence, entry.created_at, entry.delivery_id),
            )
        ]

    def referenced_legacy_artifact_paths(self) -> set[str]:
        paths: set[str] = set()
        for pending in self._legacy_delivery_evidence.values():
            artifacts = pending.message.get("artifacts") or []
            if not isinstance(artifacts, list):
                continue
            for artifact in artifacts:
                if isinstance(artifact, dict) and artifact.get("local_path"):
                    paths.add(str(artifact["local_path"]))
        return paths

    async def consume_legacy_delivery_evidence(self, delivery_id: str) -> None:
        pending = self._legacy_delivery_evidence.pop(delivery_id, None)
        if pending is None:
            return
        self._save()
        flush_task = asyncio.create_task(self.flush_pending_writes())
        cancelled = False
        while not flush_task.done():
            try:
                await asyncio.shield(flush_task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            flush_task.result()
        except BaseException:
            self._legacy_delivery_evidence[delivery_id] = pending
            raise
        if cancelled:
            raise asyncio.CancelledError

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from imagent.contracts import (
    ApplicationRef,
    ConversationBinding,
    ConversationRef,
    ThreadProjectionRoute,
    ThreadRef,
)
from imagent.projections import derive_projection_route_id

from .models import OutboundMessage
from .webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    encode_webhook_conversation,
)


class SdkMigrationState:
    """Minimal crash-safe state for the one-time legacy baseline fence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cutoffs: dict[tuple[str, str], float] = {}
        if self.path.exists():
            self._load()

    def mark(self, channel_id: str, conversation_id: str, *, cutoff: float) -> None:
        self._cutoffs[(channel_id, conversation_id)] = float(cutoff)
        self._save()

    def cutoff(self, channel_id: str, conversation_id: str) -> float | None:
        return self._cutoffs.get((channel_id, conversation_id))

    def clear(self, channel_id: str, conversation_id: str) -> None:
        if self._cutoffs.pop((channel_id, conversation_id), None) is not None:
            self._save()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported migration state")
            entries = payload.get("baseline_cutoffs")
            if not isinstance(entries, list):
                raise ValueError("invalid migration state entries")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("invalid migration state entry")
                key = (str(entry["channel_id"]), str(entry["conversation_id"]))
                self._cutoffs[key] = float(entry["cutoff"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not load SDK migration state: {self.path}") from exc

    def _save(self) -> None:
        payload = {
            "version": 1,
            "baseline_cutoffs": [
                {
                    "channel_id": key[0],
                    "conversation_id": key[1],
                    "cutoff": cutoff,
                }
                for key, cutoff in sorted(self._cutoffs.items())
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.tmp.",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


async def migrate_legacy_gateway_state(
    *,
    product_store,
    gateway_state,
    migration_state: SdkMigrationState,
    application_instance_id: str,
    native_channel_ids: frozenset[str] = frozenset(),
    clock=time.time,
) -> int:
    """Import legacy bindings/routes once without importing Agent state."""

    imported = 0
    for legacy in product_store.iter_bindings():
        conversation = (
            ConversationRef(legacy.channel_id, legacy.conversation_id)
            if legacy.channel_id in native_channel_ids
            else ConversationRef(
                WEBHOOK_CHANNEL_INSTANCE_ID,
                encode_webhook_conversation(
                    legacy.channel_id,
                    legacy.conversation_id,
                ),
            )
        )
        current = await gateway_state.get(conversation)
        if current is None:
            thread_ref = (
                ThreadRef(application_instance_id, legacy.thread_id)
                if legacy.thread_id
                else None
            )
            await gateway_state.put(
                ConversationBinding(
                    conversation_ref=conversation,
                    application_ref=ApplicationRef(application_instance_id),
                    thread_ref=thread_ref,
                    updated_at=datetime.now(UTC),
                )
            )
            imported += 1
            if thread_ref is not None:
                migration_state.mark(
                    legacy.channel_id,
                    legacy.conversation_id,
                    cutoff=clock(),
                )
        else:
            thread_ref = current.thread_ref
        if thread_ref is None:
            continue
        route = ThreadProjectionRoute(
            route_id=derive_projection_route_id(thread_ref, conversation),
            thread_ref=thread_ref,
            conversation_ref=conversation,
            updated_at=datetime.now(UTC),
        )
        await gateway_state.put_projection_route(route)
    return imported


async def recover_legacy_deliveries(*, product_store, delivery_service) -> int:
    """Converge the old durable outbox through SDK delivery, then retire entries."""

    completed = 0
    for pending in product_store.list_pending_terminal_deliveries():
        message = OutboundMessage(**pending.message)
        try:
            _outbound, delivered, _durable = (
                await delivery_service.deliver_outbound_message(message)
            )
        except Exception:
            continue
        if not delivered:
            continue
        product_store.complete_terminal_delivery(pending.delivery_id)
        await delivery_service.discard_outbound_uploads(message.artifacts)
        completed += 1
    if completed:
        await product_store.flush_pending_writes()
    return completed

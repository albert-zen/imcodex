from __future__ import annotations

import copy

from .models import PendingTerminalDelivery, TerminalDeliveryWatch


_STANDALONE_DELIVERY_ACK_LIMIT = 512


class TerminalDeliveryStoreMixin:
    """Persist native recovery watches and the independent IM delivery outbox."""

    def watch_terminal_delivery(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id or self.find_binding_by_thread_id(thread_id) is None:
            return
        key = (thread_id, turn_id)
        if key in self._terminal_delivery_watches:
            return
        self._terminal_delivery_watches[key] = TerminalDeliveryWatch(
            thread_id=thread_id,
            turn_id=turn_id,
            created_at=self.clock(),
        )
        self._save()

    def stage_terminal_delivery(
        self,
        *,
        delivery_id: str,
        thread_id: str,
        turn_id: str,
        message: dict,
    ) -> PendingTerminalDelivery:
        if not delivery_id:
            raise ValueError("delivery_id is required for terminal delivery staging")
        pending = self._pending_terminal_deliveries.get(delivery_id)
        if pending is None:
            self._next_terminal_delivery_sequence += 1
            pending = PendingTerminalDelivery(
                delivery_id=delivery_id,
                thread_id=thread_id,
                turn_id=turn_id,
                message=copy.deepcopy(message),
                created_at=self.clock(),
                sequence=self._next_terminal_delivery_sequence,
            )
            self._pending_terminal_deliveries[delivery_id] = pending
        else:
            # A staged payload owns this delivery key until acknowledgement.
            # Native replays and later route context must not replace it.
            return copy.deepcopy(pending)
        self._save()
        return copy.deepcopy(pending)

    def list_terminal_delivery_watches(
        self,
        thread_id: str | None = None,
    ) -> list[TerminalDeliveryWatch]:
        entries = self._terminal_delivery_watches.values()
        if thread_id is not None:
            entries = (entry for entry in entries if entry.thread_id == thread_id)
        return [copy.deepcopy(entry) for entry in entries]

    def list_pending_terminal_deliveries(
        self,
        thread_id: str | None = None,
    ) -> list[PendingTerminalDelivery]:
        entries = self._pending_terminal_deliveries.values()
        if thread_id is not None:
            entries = (entry for entry in entries if entry.thread_id == thread_id)
        return [
            copy.deepcopy(entry)
            for entry in sorted(
                entries,
                key=lambda entry: (entry.sequence, entry.created_at, entry.delivery_id),
            )
        ]

    def find_delivery_id_with_prefix(self, prefix: str) -> str | None:
        for delivery_id in self._pending_terminal_deliveries:
            if delivery_id.startswith(prefix):
                return delivery_id
        for delivery_id in self._acknowledged_terminal_deliveries:
            if delivery_id.startswith(prefix):
                return delivery_id
        return None

    def update_terminal_delivery_message(
        self,
        delivery_id: str,
        message: dict,
    ) -> None:
        pending = self._pending_terminal_deliveries.get(delivery_id)
        if pending is None:
            return
        pending.message = copy.deepcopy(message)
        self._save()

    def referenced_terminal_artifact_paths(self) -> set[str]:
        paths: set[str] = set()
        for pending in self._pending_terminal_deliveries.values():
            artifacts = pending.message.get("artifacts") or []
            if not isinstance(artifacts, list):
                continue
            for artifact in artifacts:
                if isinstance(artifact, dict) and artifact.get("local_path"):
                    paths.add(str(artifact["local_path"]))
        return paths

    def retry_terminal_delivery_persistence(self) -> None:
        if self._pending_terminal_deliveries:
            self._save()

    def retry_state_persistence(self) -> None:
        self._save()

    def complete_terminal_delivery(self, delivery_id: str) -> None:
        pending = self._pending_terminal_deliveries.pop(delivery_id, None)
        if pending is None:
            return
        if (pending.thread_id, pending.turn_id) == ("", ""):
            metadata = pending.message.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            self._standalone_delivery_outcomes[delivery_id] = copy.deepcopy(
                {
                    key: metadata[key]
                    for key in ("artifact_receipts", "artifact_failures")
                    if key in metadata
                }
            )
        self._acknowledged_terminal_deliveries[delivery_id] = (
            pending.thread_id,
            pending.turn_id,
        )
        self._prune_standalone_delivery_acknowledgements()
        self._save()

    def _prune_standalone_delivery_acknowledgements(self) -> None:
        standalone_ids = [
            delivery_id
            for delivery_id, terminal_key
            in self._acknowledged_terminal_deliveries.items()
            if terminal_key == ("", "")
        ]
        for delivery_id in standalone_ids[:-_STANDALONE_DELIVERY_ACK_LIMIT]:
            self._acknowledged_terminal_deliveries.pop(delivery_id, None)
            self._standalone_delivery_outcomes.pop(delivery_id, None)

    def is_terminal_delivery_acknowledged(self, delivery_id: str) -> bool:
        return delivery_id in self._acknowledged_terminal_deliveries

    def get_standalone_delivery_outcome(self, delivery_id: str) -> dict | None:
        outcome = self._standalone_delivery_outcomes.get(delivery_id)
        return None if outcome is None else copy.deepcopy(outcome)

    def has_acknowledged_terminal_delivery(
        self,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        return (thread_id, turn_id) in self._acknowledged_terminal_deliveries.values()

    def discard_terminal_watch(self, thread_id: str, turn_id: str) -> None:
        removed = self._terminal_delivery_watches.pop((thread_id, turn_id), None)
        acknowledged = {
            delivery_id: terminal_key
            for delivery_id, terminal_key
            in self._acknowledged_terminal_deliveries.items()
            if terminal_key != (thread_id, turn_id)
        }
        if (
            removed is not None
            or len(acknowledged) != len(self._acknowledged_terminal_deliveries)
        ):
            self._acknowledged_terminal_deliveries = acknowledged
            self._save()

    def _remove_terminal_deliveries_for_thread(
        self,
        thread_id: str,
        *,
        preserve_staged: bool = False,
    ) -> None:
        self._terminal_delivery_watches = {
            key: watch
            for key, watch in self._terminal_delivery_watches.items()
            if watch.thread_id != thread_id
        }
        self._pending_terminal_deliveries = {
            delivery_id: pending
            for delivery_id, pending in self._pending_terminal_deliveries.items()
            if pending.thread_id != thread_id
            or preserve_staged
        }
        self._acknowledged_terminal_deliveries = {
            delivery_id: terminal_key
            for delivery_id, terminal_key
            in self._acknowledged_terminal_deliveries.items()
            if terminal_key[0] != thread_id
        }

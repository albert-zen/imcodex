from __future__ import annotations

import copy

from .models import PendingTerminalDelivery


class TerminalDeliveryStoreMixin:
    """Persist IM delivery checkpoints without persisting native turn truth."""

    def watch_terminal_delivery(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id or self.find_binding_by_thread_id(thread_id) is None:
            return
        key = (thread_id, turn_id)
        if key in self._pending_terminal_deliveries:
            return
        self._pending_terminal_deliveries[key] = PendingTerminalDelivery(
            thread_id=thread_id,
            turn_id=turn_id,
            created_at=self.clock(),
        )
        self._save()

    def stage_terminal_delivery(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message: dict,
    ) -> PendingTerminalDelivery:
        key = (thread_id, turn_id)
        pending = self._pending_terminal_deliveries.get(key)
        if pending is None:
            pending = PendingTerminalDelivery(
                thread_id=thread_id,
                turn_id=turn_id,
                created_at=self.clock(),
            )
            self._pending_terminal_deliveries[key] = pending
        elif pending.message is not None:
            # A staged payload owns this delivery key until acknowledgement.
            # Native replays and later route context must not replace it.
            return copy.deepcopy(pending)
        pending.message = copy.deepcopy(message)
        self._save()
        return copy.deepcopy(pending)

    def list_pending_terminal_deliveries(
        self,
        thread_id: str | None = None,
    ) -> list[PendingTerminalDelivery]:
        entries = self._pending_terminal_deliveries.values()
        if thread_id is not None:
            entries = (entry for entry in entries if entry.thread_id == thread_id)
        return [copy.deepcopy(entry) for entry in entries]

    def update_terminal_delivery_message(
        self,
        thread_id: str,
        turn_id: str,
        message: dict,
    ) -> None:
        pending = self._pending_terminal_deliveries.get((thread_id, turn_id))
        if pending is None or pending.message is None:
            return
        pending.message = copy.deepcopy(message)
        self._save()

    def referenced_terminal_artifact_paths(self) -> set[str]:
        paths: set[str] = set()
        for pending in self._pending_terminal_deliveries.values():
            if pending.message is None:
                continue
            artifacts = pending.message.get("artifacts") or []
            if not isinstance(artifacts, list):
                continue
            for artifact in artifacts:
                if isinstance(artifact, dict) and artifact.get("local_path"):
                    paths.add(str(artifact["local_path"]))
        return paths

    def retry_terminal_delivery_persistence(self) -> None:
        if any(
            pending.message is not None
            for pending in self._pending_terminal_deliveries.values()
        ):
            self._save()

    def retry_state_persistence(self) -> None:
        self._save()

    def complete_terminal_delivery(self, thread_id: str, turn_id: str) -> None:
        if self._pending_terminal_deliveries.pop((thread_id, turn_id), None) is not None:
            self._save()

    def discard_terminal_watch(self, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        pending = self._pending_terminal_deliveries.get(key)
        if pending is not None and pending.message is None:
            self._pending_terminal_deliveries.pop(key, None)
            self._save()

    def _remove_terminal_deliveries_for_thread(
        self,
        thread_id: str,
        *,
        preserve_staged: bool = False,
    ) -> None:
        self._pending_terminal_deliveries = {
            key: pending
            for key, pending in self._pending_terminal_deliveries.items()
            if pending.thread_id != thread_id
            or (preserve_staged and pending.message is not None)
        }

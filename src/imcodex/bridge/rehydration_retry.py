from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence

from ..observability.runtime import emit_event


BindingKey = tuple[str, str, str]
RetryAttemptResult = frozenset[BindingKey] | None
RetryAttempt = Callable[[int, frozenset[BindingKey]], Awaitable[RetryAttemptResult]]
AsyncSleep = Callable[[float], Awaitable[None]]


class NativeWriterRehydrationRetry:
    """Retry native resume only while another App Server owns the writer.

    The controller owns no Thread or Turn state. Each attempt asks the bridge
    to reconcile only the conflicted bindings from native state again. A
    finite, capped schedule prevents a live competing writer from creating a
    permanent polling loop; exhausted work remains degraded and observable.
    """

    def __init__(
        self,
        attempt: RetryAttempt,
        *,
        delays_s: Sequence[float] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0),
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        if not delays_s:
            raise ValueError("native writer retry delays must not be empty")
        self._attempt = attempt
        self._delays_s = tuple(max(0.0, float(delay)) for delay in delays_s)
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._unresolved_kinds: dict[BindingKey, str] = {}
        self._retryable: set[BindingKey] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def schedule(
        self,
        connection_epoch: int,
        binding_keys: frozenset[BindingKey],
    ) -> None:
        await self.cancel()
        if not binding_keys:
            return
        self._unresolved_kinds = {key: "failed" for key in binding_keys}
        self._retryable = set(binding_keys)
        self._task = asyncio.create_task(
            self._run(connection_epoch),
            name=f"native-writer-rehydration-{connection_epoch}",
        )

    async def cancel(self) -> None:
        task = self._task
        self._task = None
        self._unresolved_kinds.clear()
        self._retryable.clear()
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def close(self) -> None:
        await self.cancel()

    def mark_reconciled(self, binding_key: BindingKey) -> dict[BindingKey, str]:
        """Invalidate work superseded by an ordinary exact resume/handoff."""

        channel_id, conversation_id, _thread_id = binding_key
        removed = {
            key: kind
            for key, kind in self._unresolved_kinds.items()
            if key[:2] == (channel_id, conversation_id)
        }
        for key in removed:
            self._unresolved_kinds.pop(key, None)
        self._retryable.difference_update(removed)
        return removed

    def pending_subset(
        self,
        binding_keys: frozenset[BindingKey],
    ) -> frozenset[BindingKey]:
        return frozenset(self._unresolved_kinds.keys() & binding_keys)

    def retryable_subset(
        self,
        binding_keys: frozenset[BindingKey],
    ) -> frozenset[BindingKey]:
        return frozenset(self._retryable.intersection(binding_keys))

    @property
    def retryable_count(self) -> int:
        return len(self._retryable)

    def record_retry_outcome(self, binding_key: BindingKey, outcome: str) -> None:
        """Record one native retry outcome without projecting health itself."""

        if outcome == "succeeded":
            self._unresolved_kinds.pop(binding_key, None)
            self._retryable.discard(binding_key)
        elif outcome == "superseded":
            self._unresolved_kinds.pop(binding_key, None)
            self._retryable.discard(binding_key)
        elif outcome == "retryable":
            self._unresolved_kinds[binding_key] = "failed"
            self._retryable.add(binding_key)
        elif outcome in {"failed", "unverified"}:
            self._unresolved_kinds[binding_key] = outcome
            self._retryable.discard(binding_key)
        else:
            raise ValueError(f"unsupported retry outcome: {outcome}")

    async def _run(
        self,
        connection_epoch: int,
    ) -> None:
        current = asyncio.current_task()
        binding_count = len(self._retryable)
        emit_event(
            component="bridge",
            event="bridge.thread_rehydrate.writer_retry_scheduled",
            message="Native writer conflict retry scheduled",
            data={
                "connection_epoch": connection_epoch,
                "binding_count": binding_count,
                "max_attempts": len(self._delays_s),
            },
        )
        try:
            for attempt_number, delay_s in enumerate(self._delays_s, start=1):
                await self._sleep(delay_s)
                requested = frozenset(self._retryable)
                if not requested:
                    emit_event(
                        component="bridge",
                        event="bridge.thread_rehydrate.writer_retry_converged",
                        message="Native writer conflict retry converged",
                        data={
                            "connection_epoch": connection_epoch,
                            "attempt": attempt_number - 1,
                            "resolved_by": "ordinary_reconciliation",
                        },
                    )
                    return
                try:
                    attempt_result = await self._attempt(connection_epoch, requested)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    emit_event(
                        component="bridge",
                        event="bridge.thread_rehydrate.writer_retry_stopped",
                        level="WARNING",
                        message="Native writer conflict retry stopped before convergence",
                        data={
                            "connection_epoch": connection_epoch,
                            "attempt": attempt_number,
                            "error_type": type(exc).__name__,
                        },
                    )
                    return
                if attempt_result is None:
                    emit_event(
                        component="bridge",
                        event="bridge.thread_rehydrate.writer_retry_stopped",
                        level="WARNING",
                        message="Native writer conflict retry stopped after connection changed",
                        data={
                            "connection_epoch": connection_epoch,
                            "attempt": attempt_number,
                        },
                    )
                    return
                # A normal resume may reconcile a binding while the background
                # attempt is awaiting native Codex. Never add such work back.
                for binding_key in requested - attempt_result:
                    if binding_key in self._retryable:
                        self.record_retry_outcome(binding_key, "succeeded")
                self._retryable.intersection_update(attempt_result)
                remaining = frozenset(self._retryable)
                emit_event(
                    component="bridge",
                    event="bridge.thread_rehydrate.writer_retry_attempted",
                    message="Native writer conflict retry completed",
                    data={
                        "connection_epoch": connection_epoch,
                        "attempt": attempt_number,
                        "remaining_bindings": len(remaining),
                    },
                )
                if not remaining and not self._unresolved_kinds:
                    emit_event(
                        component="bridge",
                        event="bridge.thread_rehydrate.writer_retry_converged",
                        message="Native writer conflict retry converged",
                        data={
                            "connection_epoch": connection_epoch,
                            "attempt": attempt_number,
                        },
                    )
                    return
                if not remaining:
                    emit_event(
                        component="bridge",
                        event="bridge.thread_rehydrate.writer_retry_stopped",
                        level="WARNING",
                        message="Native writer retry stopped after failure changed class",
                        data={
                            "connection_epoch": connection_epoch,
                            "attempt": attempt_number,
                            "unresolved_bindings": len(self._unresolved_kinds),
                        },
                    )
                    return
            emit_event(
                component="bridge",
                event="bridge.thread_rehydrate.writer_retry_exhausted",
                level="WARNING",
                message="Native writer conflict retry budget was exhausted",
                data={
                    "connection_epoch": connection_epoch,
                    "attempts": len(self._delays_s),
                    "remaining_bindings": len(self._retryable),
                },
            )
        finally:
            if self._task is current:
                self._task = None


class NativeWriterRehydrationMixin:
    def _handle_binding_reconciled(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> None:
        binding_key = (channel_id, conversation_id, thread_id)
        removed = self._native_writer_rehydration_retry.mark_reconciled(
            binding_key
        )
        if not removed or self._native_writer_rehydration_summary is None:
            return
        summary = dict(self._native_writer_rehydration_summary)
        exact_reconciled = binding_key in removed
        superseded_count = len(removed) - int(exact_reconciled)
        summary["total"] = max(
            0,
            int(summary.get("total") or 0) - superseded_count,
        )
        summary["succeeded"] = int(summary.get("succeeded") or 0) + int(
            exact_reconciled
        )
        failed_removed = sum(kind == "failed" for kind in removed.values())
        unverified_removed = sum(
            kind == "unverified" for kind in removed.values()
        )
        summary["failed"] = max(
            0,
            int(summary.get("failed") or 0) - failed_removed,
        )
        summary["unverified"] = max(
            0,
            int(summary.get("unverified") or 0) - unverified_removed,
        )
        remaining_conflicts = self._native_writer_rehydration_retry.retryable_count
        if remaining_conflicts:
            summary["activeWriterConflicts"] = remaining_conflicts
        else:
            summary.pop("activeWriterConflicts", None)
        self._native_writer_rehydration_summary = summary
        client = getattr(self.backend, "client", None)
        update_ready_health = getattr(client, "update_ready_health", None)
        if callable(update_ready_health):
            degraded = (
                int(summary.get("failed") or 0)
                + int(summary.get("unverified") or 0)
                + int(summary.get("deliveryFailed") or 0)
                + int(summary.get("deliveryPending") or 0)
            )
            update_ready_health(
                status="degraded" if degraded else "connected",
                rehydration=summary,
            )

    @staticmethod
    def _retryable_writer_binding_keys(result: dict) -> frozenset[BindingKey]:
        keys: set[BindingKey] = set()
        for binding in result.get("retryableBindings") or []:
            if not isinstance(binding, dict):
                continue
            channel_id = str(binding.get("channelId") or "")
            conversation_id = str(binding.get("conversationId") or "")
            thread_id = str(binding.get("threadId") or "")
            if channel_id and conversation_id and thread_id:
                keys.add((channel_id, conversation_id, thread_id))
        return frozenset(keys)

    async def _retry_native_writer_rehydration(
        self,
        connection_epoch: int,
        binding_keys: frozenset[BindingKey],
    ) -> RetryAttemptResult:
        client = getattr(self.backend, "client", None)
        connection_facts = getattr(client, "connection_facts", None)
        update_ready_health = getattr(client, "update_ready_health", None)
        if not callable(connection_facts) or not callable(update_ready_health):
            return None
        remaining: set[BindingKey] = set()
        # Reconcile one binding at a time. A normal resume can resolve one key
        # while another native call is awaiting; singleton summaries prevent
        # that interleaving from double-counting already updated health.
        for binding_key in sorted(binding_keys):
            if not self._native_writer_rehydration_retry.retryable_subset(
                frozenset({binding_key})
            ):
                continue
            before = connection_facts()
            if (
                int(before.get("connection_epoch") or 0) != connection_epoch
                or not before.get("ready")
            ):
                return None
            health, result = await self._rehydrate_current_connection(
                binding_keys={binding_key}
            )
            after = connection_facts()
            if (
                int(after.get("connection_epoch") or 0) != connection_epoch
                or not after.get("ready")
            ):
                return None
            if not self._native_writer_rehydration_retry.pending_subset(
                frozenset({binding_key})
            ):
                continue
            retryable = binding_key in self._retryable_writer_binding_keys(result)
            if retryable:
                remaining.add(binding_key)
                outcome = "retryable"
            elif not int(health["rehydration"].get("total") or 0):
                outcome = "superseded"
            elif int(health["rehydration"].get("succeeded") or 0):
                outcome = "succeeded"
            elif int(health["rehydration"].get("unverified") or 0):
                outcome = "unverified"
            else:
                outcome = "failed"
            self._native_writer_rehydration_retry.record_retry_outcome(
                binding_key,
                outcome,
            )
            remaining_count = self._native_writer_rehydration_retry.retryable_count
            summary = self._merge_writer_retry_summary(
                previous=self._native_writer_rehydration_summary,
                attempted_count=1,
                attempt=dict(health["rehydration"]),
                remaining_count=remaining_count,
            )
            self._native_writer_rehydration_summary = summary
            degraded = (
                int(summary.get("failed") or 0)
                + int(summary.get("unverified") or 0)
                + int(summary.get("deliveryFailed") or 0)
                + int(summary.get("deliveryPending") or 0)
            )
            update_ready_health(
                status="degraded" if degraded else "connected",
                rehydration=summary,
            )
        return frozenset(remaining)

    @staticmethod
    def _merge_writer_retry_summary(
        *,
        previous: dict | None,
        attempted_count: int,
        attempt: dict,
        remaining_count: int,
    ) -> dict:
        summary = dict(previous or {})
        missing_count = max(0, attempted_count - int(attempt.get("total") or 0))
        summary["total"] = max(0, int(summary.get("total") or 0) - missing_count)
        summary["succeeded"] = int(summary.get("succeeded") or 0) + int(
            attempt.get("succeeded") or 0
        )
        summary["unverified"] = int(summary.get("unverified") or 0) + int(
            attempt.get("unverified") or 0
        )
        summary["failed"] = max(
            0,
            int(summary.get("failed") or 0)
            - attempted_count
            + int(attempt.get("failed") or 0),
        )
        if remaining_count:
            summary["activeWriterConflicts"] = remaining_count
        else:
            summary.pop("activeWriterConflicts", None)
        if "deliveryPending" in attempt:
            summary["deliveryPending"] = int(attempt.get("deliveryPending") or 0)
        elif not int(attempt.get("deliveryPending") or 0):
            summary.pop("deliveryPending", None)
        if int(attempt.get("deliveryFailed") or 0):
            summary["deliveryFailed"] = max(
                int(summary.get("deliveryFailed") or 0),
                int(attempt["deliveryFailed"]),
            )
        return summary

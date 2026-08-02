from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from imagent.diagnostics import (
    ConnectionDiagnosticFacts,
    DiagnosticsSnapshot,
    QueueDiagnosticFacts,
)

from .observability.runtime import mark_http_health
from .runtime import OBSERVABILITY_IO_TIMEOUT_S


SDK_HEALTH_REFRESH_SECONDS = 1.0
SDK_MAINTENANCE_SECONDS = 2.0


@dataclass(slots=True)
class SdkRuntime:
    """Product lifecycle and operator health around one SDK Gateway."""

    gateway: object
    state: object
    client: object | None = None
    service: object | None = None
    managed_channels: list[object] = field(default_factory=list)
    observability: object | None = None
    resources: tuple[object, ...] = ()
    prepare: Callable[[], Awaitable[None]] | None = None
    maintenance: Callable[[], Awaitable[object]] | None = None
    _health_task: asyncio.Task[None] | None = None
    _maintenance_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            if self.observability is not None:
                self.observability.start()
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.starting",
                )
            if self.prepare is not None:
                await self.prepare()
            await self.gateway.start()
            if self.maintenance is not None:
                await self.maintenance()
                self._maintenance_task = asyncio.create_task(
                    self._maintenance_loop(),
                    name="imcodex-sdk-maintenance",
                )
            if self.observability is not None:
                self._observe(mark_http_health, listening=True)
                self._publish_sdk_health()
                self._health_task = asyncio.create_task(
                    self._health_loop(),
                    name="imcodex-sdk-health",
                )
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.started",
                )
                await self._flush_observability()
        except BaseException as exc:
            await self._cancel_health_task()
            await self._cancel_maintenance_task()
            with contextlib.suppress(BaseException):
                await self.gateway.stop()
            with contextlib.suppress(BaseException):
                await self._close_owned_resources()
            if self.observability is not None:
                self._observe(self.observability.update_health, status="unhealthy")
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.start_failed",
                    level="ERROR",
                    message=str(exc),
                    data={"error_type": type(exc).__name__},
                )
                await self._stop_observability()
            raise

    async def stop(self) -> None:
        errors: list[Exception] = []
        if self.observability is not None:
            self._observe(
                self.observability.emit_event,
                component="bridge",
                event="bridge.stopping",
            )
        await self._cancel_health_task()
        await self._cancel_maintenance_task()
        for operation in (self.gateway.stop, self._close_owned_resources):
            try:
                await operation()
            except asyncio.CancelledError:
                errors.append(RuntimeError("SDK runtime shutdown was cancelled"))
            except Exception as exc:
                errors.append(exc)
        if self.observability is not None:
            self._observe(mark_http_health, listening=False)
            self._observe(self.observability.update_health, status="stopped")
            self._observe(
                self.observability.emit_event,
                component="bridge",
                event="bridge.stopped",
            )
            await self._stop_observability()
        if errors:
            raise ExceptionGroup("SDK runtime shutdown failed", errors)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(SDK_HEALTH_REFRESH_SECONDS)
            self._publish_sdk_health()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(SDK_MAINTENANCE_SECONDS)
            try:
                await self.maintenance()
            except Exception:
                if self.observability is not None:
                    self._observe(self.observability.update_health, status="degraded")

    def _publish_sdk_health(self) -> None:
        try:
            snapshot = self.gateway.diagnostics_snapshot()
            payload = sdk_health_payload(snapshot)
        except Exception:
            self._observe(self.observability.update_health, status="degraded")
            return
        self._observe(
            self.observability.update_health,
            status=_sdk_health_status(snapshot),
            sdk=payload,
        )

    async def _cancel_health_task(self) -> None:
        task = self._health_task
        self._health_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_maintenance_task(self) -> None:
        task = self._maintenance_task
        self._maintenance_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _close_owned_resources(self) -> None:
        for resource in (*self.resources, self.state):
            close = getattr(resource, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            flush = getattr(resource, "flush_pending_writes", None)
            if callable(flush):
                result = flush()
                if asyncio.iscoroutine(result):
                    await result

    async def _flush_observability(self) -> None:
        flush = getattr(self.observability, "flush", None)
        if callable(flush):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(flush),
                    timeout=OBSERVABILITY_IO_TIMEOUT_S,
                )

    async def _stop_observability(self) -> None:
        stop = getattr(self.observability, "stop", None)
        if callable(stop):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.to_thread(stop),
                    timeout=OBSERVABILITY_IO_TIMEOUT_S,
                )

    @staticmethod
    def _observe(callback, *args, **kwargs) -> None:
        with contextlib.suppress(Exception):
            callback(*args, **kwargs)


def sdk_health_payload(snapshot: DiagnosticsSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "authoritative": snapshot.authoritative,
        "generated_at": snapshot.generated_at.astimezone().isoformat(),
        "gateway": {
            "accepting_inbound": snapshot.gateway.accepting_inbound,
            "starting": snapshot.gateway.starting,
            "startup_queue": _queue_payload(snapshot.gateway.startup_queue),
        },
        "applications": {
            item.application_instance_id: {
                "kind": item.kind,
                "connection": _connection_payload(item.connection),
            }
            for item in snapshot.applications
        },
        "channels": {
            item.channel_instance_id: {
                "kind": item.kind,
                "connection": _connection_payload(item.connection),
            }
            for item in snapshot.channels
        },
        "projections": {
            "worker_count": snapshot.projections.worker_count,
            "running_count": snapshot.projections.running_count,
            "retrying_count": snapshot.projections.retrying_count,
            "stopped_count": snapshot.projections.stopped_count,
            "degraded_count": snapshot.projections.degraded_count,
            "restart_count": snapshot.projections.restart_count,
            "delivery_failure_count": snapshot.projections.delivery_failure_count,
            "event_overflow_count": snapshot.projections.event_overflow_count,
            "request_recovery_degraded_count": (
                snapshot.projections.request_recovery_degraded_count
            ),
            "recovery_gap_count": snapshot.projections.recovery_gap_count,
            "recovery_gap_codes": list(snapshot.projections.recovery_gap_codes),
        },
    }


def _sdk_health_status(snapshot: DiagnosticsSnapshot) -> str:
    projection = snapshot.projections
    connections = tuple(
        item.connection for item in (*snapshot.applications, *snapshot.channels)
    )
    degraded = (
        projection.degraded_count
        or projection.delivery_failure_count
        or projection.event_overflow_count
        or projection.request_recovery_degraded_count
        or projection.recovery_gap_count
        or any(connection.worker_degraded for connection in connections if connection)
    )
    return "degraded" if degraded else "healthy"


def _queue_payload(queue: QueueDiagnosticFacts) -> dict[str, Any]:
    return {
        "name": queue.name.value,
        "capacity": queue.capacity,
        "depth": queue.depth,
        "overflow_count": queue.overflow_count,
    }


def _connection_payload(connection: ConnectionDiagnosticFacts | None) -> dict[str, Any] | None:
    if connection is None:
        return None
    return {
        "state": connection.state.value,
        "connection_epoch": connection.connection_epoch,
        "reconnect_count": connection.reconnect_count,
        "worker_running": connection.worker_running,
        "worker_degraded": connection.worker_degraded,
        "last_failure_code": (
            connection.last_failure_code.value if connection.last_failure_code else None
        ),
        "queues": [_queue_payload(queue) for queue in connection.queues],
    }

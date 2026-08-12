from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from imagent.gateway.diagnostics import (
    ConnectionDiagnosticFacts,
    ConnectionDiagnosticState,
    DiagnosticsSnapshot,
    QueueDiagnosticFacts,
)

from .observability.runtime import mark_http_health


OBSERVABILITY_IO_TIMEOUT_S = 4.0
SDK_HEALTH_REFRESH_SECONDS = 1.0


@dataclass(slots=True)
class SdkRuntime:
    """Product lifecycle around one SDK-owned Gateway graph."""

    gateway: object
    state: object
    client: object | None = None
    service: object | None = None
    managed_channels: list[object] = field(default_factory=list)
    observability: object | None = None
    resources: tuple[object, ...] = ()
    _health_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            if self.observability is not None:
                self.observability.start()
                self._observe(self.observability.emit_event, component="bridge", event="bridge.starting")
            await self.gateway.start()
            if self.service is not None:
                start_service = getattr(self.service, "start", None)
                if callable(start_service):
                    result = start_service()
                    if asyncio.iscoroutine(result):
                        await result
            if self.observability is not None:
                self._observe(mark_http_health, listening=True)
                self._publish_sdk_health()
                self._health_task = asyncio.create_task(
                    self._health_loop(),
                    name="imcodex-sdk-health",
                )
                self._observe(self.observability.emit_event, component="bridge", event="bridge.started")
                await self._flush_observability()
        except BaseException as exc:
            await self._cancel_health_task()
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
            self._observe(self.observability.emit_event, component="bridge", event="bridge.stopping")
        await self._cancel_health_task()
        if self.service is not None:
            stop_service = getattr(self.service, "stop", None)
            if callable(stop_service):
                try:
                    result = stop_service()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    errors.append(RuntimeError("durable delivery shutdown was cancelled"))
                except Exception as exc:
                    errors.append(exc)
        try:
            await self.gateway.stop()
        except asyncio.CancelledError:
            errors.append(RuntimeError("SDK Gateway shutdown was cancelled"))
        except Exception as exc:
            errors.append(exc)
        try:
            await self._close_owned_resources()
        except asyncio.CancelledError:
            errors.append(RuntimeError("SDK state shutdown was cancelled"))
        except Exception as exc:
            errors.append(exc)
        if self.observability is not None:
            self._observe(mark_http_health, listening=False)
            self._observe(self.observability.update_health, status="stopped")
            self._observe(self.observability.emit_event, component="bridge", event="bridge.stopped")
            await self._stop_observability()
        if errors:
            raise ExceptionGroup("SDK runtime shutdown failed", errors)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(SDK_HEALTH_REFRESH_SECONDS)
            self._publish_sdk_health()

    def _publish_sdk_health(self) -> None:
        if self.observability is None:
            return
        try:
            snapshot = self.gateway.diagnostics()
            payload = sdk_health_payload(snapshot)
        except Exception as exc:
            self._observe(
                self.observability.update_health,
                status="degraded",
                sdk={"error": type(exc).__name__},
            )
            return
        delivery = self._delivery_health()
        status = _sdk_health_status(snapshot)
        if delivery.get("status") == "degraded":
            status = "degraded"
        self._observe(
            self.observability.update_health,
            status=status,
            sdk=payload,
            appserver=_appserver_health(snapshot, client=self.client),
            delivery=delivery,
        )

    def _delivery_health(self) -> dict[str, object]:
        provider = getattr(self.service, "delivery_health", None)
        if not callable(provider):
            return {"status": "healthy", "pending_count": 0}
        try:
            return dict(provider())
        except Exception as exc:
            return {
                "status": "degraded",
                "pending_count": None,
                "error": type(exc).__name__,
            }

    async def _cancel_health_task(self) -> None:
        task = self._health_task
        self._health_task = None
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

    async def _flush_observability(self) -> None:
        flush = getattr(self.observability, "flush", None)
        if callable(flush):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(flush), timeout=OBSERVABILITY_IO_TIMEOUT_S)

    async def _stop_observability(self) -> None:
        stop = getattr(self.observability, "stop", None)
        if callable(stop):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.to_thread(stop), timeout=OBSERVABILITY_IO_TIMEOUT_S)

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
            "request_recovery_degraded_count": snapshot.projections.request_recovery_degraded_count,
            "recovery_gap_count": snapshot.projections.recovery_gap_count,
            "recovery_gap_codes": list(snapshot.projections.recovery_gap_codes),
        },
    }


def _sdk_health_status(snapshot: DiagnosticsSnapshot) -> str:
    projection = snapshot.projections
    connections = tuple(item.connection for item in (*snapshot.applications, *snapshot.channels))
    degraded = (
        not snapshot.gateway.accepting_inbound
        or snapshot.gateway.starting
        or projection.degraded_count
        or projection.delivery_failure_count
        or projection.event_overflow_count
        or projection.request_recovery_degraded_count
        or projection.recovery_gap_count
        or any(
            connection.worker_degraded or connection.state is not ConnectionDiagnosticState.READY
            for connection in connections
            if connection is not None
        )
    )
    return "degraded" if degraded else "healthy"


def _appserver_health(snapshot: DiagnosticsSnapshot, *, client=None) -> dict[str, Any]:
    application = next(
        (item for item in snapshot.applications if item.kind in {"codex", "appserver"}),
        snapshot.applications[0] if snapshot.applications else None,
    )
    connection = application.connection if application is not None else None
    state = connection.state if connection is not None else ConnectionDiagnosticState.DISCONNECTED
    topology: dict[str, Any] = {}
    provider = getattr(client, "connection_facts", None)
    if callable(provider):
        facts = dict(provider())
        topology = {
            key: facts[key]
            for key in (
                "connected",
                "ready",
                "status",
                "mode",
                "ownership",
                "transport",
                "reconnect_enabled",
                "local_image_paths",
                "connection_epoch",
            )
            if key in facts
        }
    return {
        "mode": "sdk-application",
        "ownership": "sdk-application",
        "transport": "sdk-owned",
        "endpoint": "(redacted by SDK diagnostics)",
        "connected": state is ConnectionDiagnosticState.READY,
        "ready": state is ConnectionDiagnosticState.READY,
        "status": "connected" if state is ConnectionDiagnosticState.READY else state.value,
        "connection_epoch": connection.connection_epoch if connection else 0,
        **topology,
    }


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
        "last_failure_code": connection.last_failure_code.value if connection.last_failure_code else None,
        "queues": [_queue_payload(queue) for queue in connection.queues],
    }

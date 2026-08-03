from __future__ import annotations

from datetime import UTC, datetime

import pytest
from imagent.diagnostics import (
    ApplicationDiagnosticFacts,
    ChannelDiagnosticFacts,
    ConnectionDiagnosticFacts,
    ConnectionDiagnosticState,
    DiagnosticsSnapshot,
    GatewayDiagnosticFacts,
    ProjectionDiagnosticFacts,
    QueueDiagnosticFacts,
    QueueDiagnosticName,
)

from imcodex.sdk_runtime import SdkRuntime, sdk_health_payload


def _snapshot(*, degraded: bool = False) -> DiagnosticsSnapshot:
    application_connection = ConnectionDiagnosticFacts(
        state=ConnectionDiagnosticState.READY,
        connection_epoch=2,
        reconnect_count=1,
        worker_running=True,
        worker_degraded=degraded,
        queues=(
            QueueDiagnosticFacts(
                name=QueueDiagnosticName.NOTIFICATION,
                capacity=32,
                depth=3,
            ),
        ),
    )
    channel_connection = ConnectionDiagnosticFacts(
        state=ConnectionDiagnosticState.READY,
        connection_epoch=2,
        reconnect_count=1,
        worker_running=True,
        worker_degraded=degraded,
        queues=(
            QueueDiagnosticFacts(
                name=QueueDiagnosticName.CHANNEL_INBOUND,
                capacity=32,
                depth=3,
            ),
        ),
    )
    return DiagnosticsSnapshot(
        applications=(
            ApplicationDiagnosticFacts("codex-main", "codex", application_connection),
        ),
        channels=(ChannelDiagnosticFacts("telegram", "telegram", channel_connection),),
        projections=ProjectionDiagnosticFacts(degraded_count=int(degraded)),
        gateway=GatewayDiagnosticFacts(
            accepting_inbound=True,
            starting=False,
            startup_queue=QueueDiagnosticFacts(
                name=QueueDiagnosticName.GATEWAY_STARTUP,
                capacity=16,
                depth=0,
            ),
        ),
        generated_at=datetime(2026, 8, 2, tzinfo=UTC),
    )


def test_sdk_health_payload_is_redacted_and_operator_friendly() -> None:
    payload = sdk_health_payload(_snapshot())

    assert payload["schema_version"] == 8
    assert payload["authoritative"] is False
    assert payload["applications"]["codex-main"]["connection"]["state"] == "ready"
    assert payload["channels"]["telegram"]["connection"]["reconnect_count"] == 1
    assert payload["projections"]["worker_count"] == 0


@pytest.mark.asyncio
async def test_sdk_runtime_owns_gateway_state_and_health_lifecycle() -> None:
    calls: list[str] = []
    health: list[dict] = []

    class Gateway:
        async def start(self) -> None:
            calls.append("gateway.start")

        async def stop(self) -> None:
            calls.append("gateway.stop")

        def diagnostics_snapshot(self):
            return _snapshot(degraded=True)

    class State:
        async def close(self) -> None:
            calls.append("state.close")

    class Observability:
        def start(self) -> None:
            calls.append("obs.start")

        def emit_event(self, *, component, event, **kwargs) -> None:
            del component, kwargs
            calls.append(f"obs.{event}")

        def update_health(self, **changes) -> None:
            health.append(changes)

        def flush(self) -> None:
            calls.append("obs.flush")

        def stop(self) -> None:
            calls.append("obs.stop")

    runtime = SdkRuntime(
        gateway=Gateway(),
        state=State(),
        observability=Observability(),
    )

    await runtime.start()
    await runtime.stop()

    assert calls == [
        "obs.start",
        "obs.bridge.starting",
        "gateway.start",
        "obs.bridge.started",
        "obs.flush",
        "obs.bridge.stopping",
        "gateway.stop",
        "state.close",
        "obs.bridge.stopped",
        "obs.stop",
    ]
    assert health[0]["status"] == "degraded"
    assert health[0]["sdk"]["gateway"]["accepting_inbound"] is True
    assert health[-1] == {"status": "stopped"}

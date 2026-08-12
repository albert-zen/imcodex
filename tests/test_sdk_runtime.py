from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from imcodex.sdk_runtime import SdkRuntime, sdk_health_payload


def test_sdk_health_payload_projects_authoritative_gateway_diagnostics() -> None:
    class Queue:
        name = type("Name", (), {"value": "gateway_startup"})()
        capacity = 8
        depth = 1
        overflow_count = 0

    class GatewayFacts:
        accepting_inbound = True
        starting = False
        startup_queue = Queue()

    class ProjectionFacts:
        worker_count = 1
        running_count = 1
        retrying_count = 0
        stopped_count = 0
        degraded_count = 0
        restart_count = 0
        delivery_failure_count = 0
        event_overflow_count = 0
        request_recovery_degraded_count = 0
        recovery_gap_count = 0
        recovery_gap_codes = ()

    class Snapshot:
        schema_version = 8
        authoritative = True
        generated_at = datetime.now(UTC)
        gateway = GatewayFacts()
        projections = ProjectionFacts()
        applications = ()
        channels = ()

    payload = sdk_health_payload(Snapshot())
    assert payload["authoritative"] is True
    assert payload["gateway"]["accepting_inbound"] is True
    assert payload["projections"]["worker_count"] == 1


async def test_sdk_runtime_starts_delivery_drain_for_every_gateway_mode() -> None:
    events = []

    class Gateway:
        async def start(self):
            events.append("gateway.start")

        async def stop(self):
            events.append("gateway.stop")

    class Service:
        async def start(self):
            events.append("service.start")

        async def stop(self):
            events.append("service.stop")

        async def close(self):
            events.append("service.close")

    class State:
        def close(self):
            events.append("state.close")

    service = Service()
    runtime = SdkRuntime(
        gateway=Gateway(),
        state=State(),
        service=service,
        resources=(service,),
    )

    await runtime.start()
    await runtime.stop()

    assert events == [
        "gateway.start",
        "service.start",
        "service.stop",
        "gateway.stop",
        "service.close",
        "state.close",
    ]


def test_pending_delivery_degrades_runtime_health() -> None:
    updates = []

    class Queue:
        name = SimpleNamespace(value="gateway_startup")
        capacity = 8
        depth = 0
        overflow_count = 0

    class Snapshot:
        schema_version = 8
        authoritative = True
        generated_at = datetime.now(UTC)
        gateway = SimpleNamespace(
            accepting_inbound=True,
            starting=False,
            startup_queue=Queue(),
        )
        projections = SimpleNamespace(
            worker_count=0,
            running_count=0,
            retrying_count=0,
            stopped_count=0,
            degraded_count=0,
            restart_count=0,
            delivery_failure_count=0,
            event_overflow_count=0,
            request_recovery_degraded_count=0,
            recovery_gap_count=0,
            recovery_gap_codes=(),
        )
        applications = ()
        channels = ()

    runtime = SdkRuntime(
        gateway=SimpleNamespace(diagnostics=lambda: Snapshot()),
        state=SimpleNamespace(),
        service=SimpleNamespace(
            delivery_health=lambda: {"status": "degraded", "pending_count": 1}
        ),
        observability=SimpleNamespace(update_health=lambda **values: updates.append(values)),
    )

    runtime._publish_sdk_health()

    assert updates[0]["status"] == "degraded"
    assert updates[0]["delivery"]["pending_count"] == 1

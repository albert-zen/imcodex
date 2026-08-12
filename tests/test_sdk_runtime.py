from __future__ import annotations

from datetime import UTC, datetime

from imcodex.sdk_runtime import sdk_health_payload


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

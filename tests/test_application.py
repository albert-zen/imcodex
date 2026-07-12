from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from imcodex.application import create_application
from imcodex.config import Settings
from imcodex.models import OutboundMessage


class _Service:
    async def handle_inbound(self, message):
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def _client_with_address(app, host: str) -> TestClient:
    class ClientAddress:
        async def __call__(self, scope, receive, send) -> None:
            if scope.get("type") == "http":
                scope = {**scope, "client": (host, 50000)}
            await app(scope, receive, send)

    return TestClient(ClientAddress())


def test_application_factory_uses_resolved_webhook_token(monkeypatch) -> None:
    settings = SimpleNamespace(
        inbound_webhook_token="factory-secret",
        debug_api_enabled=False,
    )
    runtime = SimpleNamespace(service=_Service())
    observed_settings: list[object] = []

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda _cls: settings))

    def build_runtime(resolved_settings, *, settings_source):
        observed_settings.append((resolved_settings, settings_source))
        return runtime

    monkeypatch.setattr("imcodex.application.build_runtime", build_runtime)
    client = TestClient(create_application())
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "hello",
    }

    assert client.post("/api/channels/webhook/inbound", json=body).status_code == 401
    assert (
        client.post(
            "/api/channels/webhook/inbound",
            headers={"Authorization": "Bearer factory-secret"},
            json=body,
        ).status_code
        == 200
    )
    assert observed_settings == [(settings, "environment")]


def test_application_health_identifies_the_running_bridge_process() -> None:
    settings = SimpleNamespace(
        inbound_webhook_token="",
        debug_api_enabled=False,
    )
    context = SimpleNamespace(pid=43210, instance_id="instance-43210")
    runtime = SimpleNamespace(
        service=_Service(),
        observability=SimpleNamespace(context=context),
    )

    response = TestClient(create_application(settings=settings, runtime=runtime)).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "kind": "imcodex.bridge",
        "status": "healthy",
        "pid": 43210,
        "instanceId": "instance-43210",
    }


def test_application_graceful_shutdown_is_loopback_and_instance_bound() -> None:
    settings = SimpleNamespace(
        inbound_webhook_token="",
        debug_api_enabled=False,
    )
    context = SimpleNamespace(pid=43210, instance_id="instance-43210")
    runtime = SimpleNamespace(
        service=_Service(),
        observability=SimpleNamespace(context=context),
    )
    app = create_application(settings=settings, runtime=runtime)
    shutdowns: list[str] = []
    app.state.request_shutdown = lambda: shutdowns.append("requested")
    loopback = _client_with_address(app, "127.0.0.1")

    rejected = loopback.post("/_imcodex/ops/shutdown")
    accepted = loopback.post(
        "/_imcodex/ops/shutdown",
        headers={"x-imcodex-instance": "instance-43210"},
    )
    remote = _client_with_address(app, "192.0.2.50").post(
        "/_imcodex/ops/shutdown",
        headers={"x-imcodex-instance": "instance-43210"},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "shutting_down"}
    assert remote.status_code == 403
    assert shutdowns == ["requested"]

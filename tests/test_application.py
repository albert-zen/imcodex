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


def test_application_factory_uses_resolved_webhook_token(monkeypatch) -> None:
    settings = SimpleNamespace(
        inbound_webhook_token="factory-secret",
        debug_api_enabled=False,
    )
    runtime = SimpleNamespace(service=_Service())
    observed_settings: list[object] = []

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda _cls: settings))

    def build_runtime(resolved_settings):
        observed_settings.append(resolved_settings)
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
    assert observed_settings == [settings]

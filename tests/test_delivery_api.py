from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imcodex.delivery_api import (
    DELIVERY_PATH,
    DELIVERY_TOKEN_HEADER,
    install_delivery_route,
)
from imcodex.delivery_artifacts import DeliveryArtifactStager


class _Service:
    def __init__(self, root: Path) -> None:
        self.stager = DeliveryArtifactStager(root / "outbound-media")
        self.received = []

    def can_deliver_outbound(self, channel_id: str) -> bool:
        return channel_id == "telegram"

    def validate_outbound_message(self, _message) -> None:
        return None

    async def stage_outbound_upload(self, content: bytes, **kwargs):
        return self.stager.stage_upload(content, **kwargs)

    async def discard_outbound_uploads(self, artifacts) -> None:
        self.stager.release(artifacts)
        self.stager.cleanup_unreferenced(set())

    async def deliver_outbound_message(self, message):
        self.received.append(message)
        return [message], True, True


def _app(tmp_path: Path):
    app = FastAPI()
    service = _Service(tmp_path)
    runtime = SimpleNamespace(
        service=service,
        observability=SimpleNamespace(context=SimpleNamespace(instance_id="instance-1")),
    )
    credential = install_delivery_route(app, runtime, run_dir=tmp_path / "run")
    credential.publish()
    return app, service, credential


def _client(app: FastAPI) -> TestClient:
    class Loopback:
        async def __call__(self, scope, receive, send) -> None:
            if scope.get("type") == "http":
                scope = {**scope, "client": ("127.0.0.1", 51000)}
            await app(scope, receive, send)

    return TestClient(Loopback(), base_url="http://127.0.0.1")


def test_delivery_endpoint_stages_and_delivers_explicit_route(tmp_path: Path) -> None:
    app, service, credential = _app(tmp_path)
    payload = {
        "channel_id": "telegram",
        "conversation_id": "chat-1",
        "delivery_id": "delivery-1",
        "text": "hello",
        "artifacts": [{"kind": "file", "filename": "result.txt"}],
    }
    with _client(app) as client:
        response = client.post(
            DELIVERY_PATH,
            headers={
                "x-imcodex-instance": "instance-1",
                DELIVERY_TOKEN_HEADER: credential.token,
            },
            data={"payload": json.dumps(payload)},
            files={"artifacts": ("result.txt", b"artifact", "text/plain")},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert service.received[0].artifacts[0].sha256


def test_delivery_endpoint_rejects_missing_local_credential(tmp_path: Path) -> None:
    app, _service, credential = _app(tmp_path)
    payload = {
        "channel_id": "telegram",
        "conversation_id": "chat-1",
        "delivery_id": "delivery-2",
        "text": "hello",
    }
    with _client(app) as client:
        response = client.post(
            DELIVERY_PATH,
            headers={"x-imcodex-instance": "instance-1"},
            data={"payload": json.dumps(payload)},
        )
    assert response.status_code == 403

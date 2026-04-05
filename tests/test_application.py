from __future__ import annotations

from fastapi.testclient import TestClient

from imcodex.application import create_application


class FakeService:
    async def handle_inbound(self, message):
        return []


class FakeRuntime:
    def __init__(self) -> None:
        self.service = FakeService()
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def test_application_lifespan_starts_and_stops_runtime() -> None:
    runtime = FakeRuntime()

    with TestClient(create_application(runtime=runtime)) as client:
        response = client.post(
            "/api/channels/webhook/inbound",
            json={
                "channel_id": "demo",
                "conversation_id": "conv-1",
                "user_id": "u1",
                "message_id": "m1",
                "text": "/status",
            },
        )
        assert response.status_code == 200
        assert runtime.started == 1

    assert runtime.stopped == 1

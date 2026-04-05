from __future__ import annotations

from fastapi.testclient import TestClient

from imcodex.channels import create_app
from imcodex.models import InboundMessage


class FakeService:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage):
        self.messages.append(message)
        return [
            {
                "channel_id": message.channel_id,
                "conversation_id": message.conversation_id,
                "message_type": "status",
                "text": "accepted",
                "ticket_id": None,
                "metadata": {},
            }
        ]


def test_inbound_webhook_routes_message_to_service() -> None:
    service = FakeService()
    client = TestClient(create_app(service))

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
    body = response.json()
    assert body["messages"][0]["text"] == "accepted"
    assert service.messages[0].text == "/status"

from __future__ import annotations

from fastapi.testclient import TestClient

from imcodex.channels import QQChannelAdapter, create_app
from imcodex.models import OutboundMessage


class StubService:
    async def handle_inbound(self, message):
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def test_webhook_inbound_returns_messages() -> None:
    client = TestClient(create_app(StubService()))

    response = client.post(
        "/api/channels/webhook/inbound",
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["message_type"] == "accepted"


def test_qq_adapter_normalizes_group_mention_message() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        service=object(),
    )

    inbound = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "<@123>  inspect repo",
            "group_openid": "group-1",
            "author": {"member_openid": "user-1"},
        },
    )

    assert inbound is not None
    assert inbound.conversation_id == "group:group-1"
    assert inbound.text == "inspect repo"

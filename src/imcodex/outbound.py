from __future__ import annotations

import httpx

from .models import OutboundMessage


class WebhookOutboundSink:
    def __init__(self, outbound_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.outbound_url = outbound_url
        self.client = client or httpx.AsyncClient()

    async def send_message(self, message: OutboundMessage) -> None:
        await self.client.post(
            self.outbound_url,
            json={
                "channel_id": message.channel_id,
                "conversation_id": message.conversation_id,
                "message_type": message.message_type,
                "text": message.text,
                "ticket_id": message.ticket_id,
                "metadata": message.metadata,
            },
        )


class MultiplexOutboundSink:
    def __init__(
        self,
        *,
        channel_sinks: dict[str, object] | None = None,
        default_sink: object | None = None,
    ) -> None:
        self.channel_sinks = channel_sinks or {}
        self.default_sink = default_sink

    async def send_message(self, message: OutboundMessage) -> None:
        sink = self.channel_sinks.get(message.channel_id) or self.default_sink
        if sink is None:
            return
        await sink.send_message(message)

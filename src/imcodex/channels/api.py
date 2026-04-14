from __future__ import annotations

import logging

from fastapi import FastAPI

from ..models import InboundMessage

logger = logging.getLogger(__name__)


def create_app(service) -> FastAPI:
    app = FastAPI()

    @app.post("/api/channels/webhook/inbound")
    async def webhook_inbound(message: InboundMessage) -> dict:
        logger.info(
            "Webhook inbound received channel_id=%s conversation_id=%s message_id=%s",
            message.channel_id,
            message.conversation_id,
            message.message_id,
        )
        outbound = await service.handle_inbound(message)
        return {"messages": outbound}

    return app

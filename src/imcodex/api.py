from __future__ import annotations

from fastapi import FastAPI

from .models import InboundMessage


def create_app(service) -> FastAPI:
    app = FastAPI()

    @app.post("/api/channels/webhook/inbound")
    async def webhook_inbound(message: InboundMessage) -> dict:
        outbound = await service.handle_inbound(message)
        return {"messages": outbound}

    return app

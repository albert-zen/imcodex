from __future__ import annotations

from dataclasses import asdict
import secrets

from fastapi import FastAPI, Header, HTTPException, Request

from ..models import InboundMessage


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def create_app(service, *, inbound_token: str = "") -> FastAPI:
    app = FastAPI()

    @app.post("/api/channels/webhook/inbound")
    async def webhook_inbound(
        message: InboundMessage,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _authorize_inbound_webhook(
            request=request,
            authorization=authorization,
            configured_token=inbound_token,
        )
        outbound = await service.handle_inbound(message)
        return {"messages": [asdict(item) for item in outbound]}

    return app


def _authorize_inbound_webhook(
    *,
    request: Request,
    authorization: str | None,
    configured_token: str,
) -> None:
    token = configured_token.strip()
    if token:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Invalid inbound webhook credentials.")
        return
    client_host = request.client.host if request.client is not None else ""
    if client_host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Remote inbound webhook access requires IMCODEX_INBOUND_WEBHOOK_TOKEN.",
        )

from __future__ import annotations

from dataclasses import asdict
import ipaddress
import re
import secrets

from fastapi import FastAPI, HTTPException
from starlette.responses import JSONResponse

from ..models import InboundMessage, OutboundMessage
from .middleware import UnifiedChannelMiddleware
from .registry import BUILTIN_CHANNEL_IDS


INBOUND_WEBHOOK_PATH = "/api/channels/webhook/inbound"
MAX_INBOUND_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_INBOUND_TEXT_CHARS = 32 * 1024
WEBHOOK_ID_LIMITS = {
    "channel_id": 64,
    "conversation_id": 1024,
    "user_id": 512,
    "message_id": 512,
}
CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class _InboundWebhookGuard:
    """Authenticate and bound the webhook before FastAPI reads its body."""

    def __init__(self, app, *, configured_token: str) -> None:
        self.app = app
        self.configured_token = configured_token.strip()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != INBOUND_WEBHOOK_PATH:
            await self.app(scope, receive, send)
            return

        authorization = self._header(scope, b"authorization")
        denial = self._authorization_denial(scope=scope, authorization=authorization)
        if denial is not None:
            status_code, detail = denial
            await JSONResponse({"detail": detail}, status_code=status_code)(scope, receive, send)
            return

        content_length = self._header(scope, b"content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(scope, receive, send)
                return
            if declared_size > MAX_INBOUND_WEBHOOK_BODY_BYTES:
                await self._send_too_large(scope, receive, send)
                return

        chunks: list[bytes] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            body = message.get("body") or b""
            received += len(body)
            if received > MAX_INBOUND_WEBHOOK_BODY_BYTES:
                await self._send_too_large(scope, receive, send)
                return
            chunks.append(body)
            more_body = bool(message.get("more_body"))

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay_receive, send)

    async def _send_too_large(self, scope, receive, send) -> None:
        await JSONResponse(
            {"detail": "Inbound webhook body is too large."},
            status_code=413,
        )(scope, receive, send)

    def _authorization_denial(
        self,
        *,
        scope,
        authorization: bytes,
    ) -> tuple[int, str] | None:
        if self.configured_token:
            scheme, _, supplied = authorization.partition(b" ")
            if scheme.lower() != b"bearer" or not secrets.compare_digest(
                supplied,
                self.configured_token.encode("utf-8"),
            ):
                return 401, "Invalid inbound webhook credentials."
            return None
        client = scope.get("client")
        client_host = str(client[0]) if isinstance(client, (tuple, list)) and client else ""
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            return (
                403,
                "Remote inbound webhook access requires IMCODEX_INBOUND_WEBHOOK_TOKEN.",
            )
        return None

    @staticmethod
    def _header(scope, name: bytes) -> bytes:
        for key, value in scope.get("headers") or ():
            if key.lower() == name:
                return value
        return b""


class _WebhookResponseAdapter:
    def __init__(
        self,
        channel_id: str,
        *,
        outbound_sink=None,
    ) -> None:
        self.channel_id = channel_id
        self.messages: list[OutboundMessage] = []
        self.outbound_sink = outbound_sink

    async def send_message(self, message: OutboundMessage) -> None:
        self.messages.append(message)

    async def after_inbound_committed(self) -> None:
        if self.outbound_sink is not None:
            for message in self.messages:
                await self.outbound_sink.send_message(message)


def create_app(service, *, inbound_token: str = "") -> FastAPI:
    app = FastAPI()
    middleware = UnifiedChannelMiddleware(service=service)

    @app.post(INBOUND_WEBHOOK_PATH)
    async def webhook_inbound(message: InboundMessage) -> dict:
        for field_name, limit in WEBHOOK_ID_LIMITS.items():
            value = str(getattr(message, field_name) or "")
            if not value.strip():
                raise HTTPException(status_code=422, detail=f"{field_name} must not be empty.")
            if len(value) > limit:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} exceeds the {limit}-character limit.",
                )
        if CHANNEL_ID_PATTERN.fullmatch(message.channel_id) is None:
            raise HTTPException(status_code=422, detail="channel_id contains unsupported characters.")
        if message.channel_id in BUILTIN_CHANNEL_IDS:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The generic webhook cannot claim a built-in channel ID. Use a dedicated gateway channel namespace."
                ),
            )
        if not message.text.strip():
            raise HTTPException(status_code=422, detail="Inbound message text must not be empty.")
        if len(message.text) > MAX_INBOUND_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="Inbound message text is too large.")
        adapter = _WebhookResponseAdapter(
            message.channel_id,
            outbound_sink=getattr(service, "outbound_sink", None),
        )
        await middleware.handle_inbound(
            adapter,
            message,
            reply_to_message_id=message.reply_to_message_id or message.message_id,
        )
        return {"messages": [asdict(item) for item in adapter.messages]}

    app.add_middleware(_InboundWebhookGuard, configured_token=inbound_token)
    return app

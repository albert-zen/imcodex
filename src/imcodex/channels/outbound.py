from __future__ import annotations

import asyncio
import ipaddress
import math

import httpx

from .registry import BUILTIN_CHANNEL_IDS


class WebhookOutboundSink:
    def __init__(
        self,
        outbound_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        bearer_token: str = "",
        max_attempts: int = 3,
        sleep=asyncio.sleep,
    ) -> None:
        self.outbound_url = outbound_url
        self.client = client
        self.bearer_token = bearer_token.strip()
        self.max_attempts = max(1, int(max_attempts))
        self.sleep = sleep
        self._validate_endpoint()

    async def send_message(self, message) -> None:
        payload = {
            "channel_id": message.channel_id,
            "conversation_id": message.conversation_id,
            "message_type": message.message_type,
            "text": message.text,
            "request_id": message.request_id,
            "metadata": message.metadata,
        }
        headers = {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else None
        if self.client is not None:
            await self._post_with_retries(self.client, payload=payload, headers=headers)
            return
        async with httpx.AsyncClient() as client:
            await self._post_with_retries(client, payload=payload, headers=headers)

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        *,
        payload: dict,
        headers: dict[str, str] | None,
    ) -> None:
        delivery_id = str(payload.get("metadata", {}).get("delivery_id") or "")
        attempts = self.max_attempts if delivery_id else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(
                    self.outbound_url,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError:
                if attempt >= attempts:
                    raise
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if response.is_success:
                return
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts:
                    await self.sleep(self._retry_after(response, attempt))
                    continue
            response.raise_for_status()

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        try:
            delay = float(response.headers.get("Retry-After", ""))
        except ValueError:
            return min(2 ** (attempt - 1), 4)
        if not math.isfinite(delay):
            return min(2 ** (attempt - 1), 4)
        return min(5.0, max(0.0, delay))

    def _validate_endpoint(self) -> None:
        try:
            url = httpx.URL(self.outbound_url)
        except ValueError as exc:
            raise ValueError("IMCODEX_OUTBOUND_URL is invalid.") from exc
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("IMCODEX_OUTBOUND_URL must be an HTTP(S) URL.")
        if url.username or url.password:
            raise ValueError("IMCODEX_OUTBOUND_URL must not contain userinfo credentials.")
        host = url.host.rstrip(".").lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback and url.scheme != "https":
            raise ValueError("Remote IMCODEX_OUTBOUND_URL requires HTTPS.")
        if not loopback and not self.bearer_token:
            raise ValueError("Remote IMCODEX_OUTBOUND_URL requires IMCODEX_OUTBOUND_WEBHOOK_TOKEN.")


class MultiplexOutboundSink:
    def __init__(
        self,
        *,
        channel_sinks: dict[str, object] | None = None,
        default_sink: object | None = None,
    ) -> None:
        self.channel_sinks = channel_sinks or {}
        self.default_sink = default_sink

    async def send_message(self, message) -> None:
        sink = self.channel_sinks.get(message.channel_id)
        if sink is None and message.channel_id in BUILTIN_CHANNEL_IDS:
            raise RuntimeError(f"Built-in channel {message.channel_id!r} is not enabled; refusing fallback delivery.")
        sink = sink or self.default_sink
        if sink is None:
            return
        await sink.send_message(message)

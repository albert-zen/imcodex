from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from pathlib import Path

import httpx

from .registry import BUILTIN_CHANNEL_IDS
from .artifacts import (
    PermanentArtifactDeliveryError,
    append_artifact_failures,
    read_managed_artifact,
)


class WebhookOutboundSink:
    supports_outbound_artifacts = True

    def __init__(
        self,
        outbound_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        bearer_token: str = "",
        outbound_media_dir: str | Path = ".imcodex/outbound-media",
        max_attempts: int = 3,
        sleep=asyncio.sleep,
    ) -> None:
        self.outbound_url = outbound_url
        self.client = client
        self.bearer_token = bearer_token.strip()
        self.outbound_media_dir = Path(outbound_media_dir).resolve()
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
        files = None
        data = None
        if message.artifacts:
            uploads = []
            manifest = []
            deliverable = []
            failures: list[str] = []
            for artifact in message.artifacts:
                try:
                    _source, content = await read_managed_artifact(
                        artifact,
                        root=self.outbound_media_dir,
                    )
                except PermanentArtifactDeliveryError as exc:
                    failures.append(f"{artifact.filename}: {exc}")
                    continue
                deliverable.append(artifact)
                uploads.append(
                    (
                        "artifacts",
                        (artifact.filename, content, artifact.content_type),
                    )
                )
                manifest.append(
                    {
                        "kind": artifact.kind,
                        "filename": artifact.filename,
                        "content_type": artifact.content_type,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                    }
                )
            message.artifacts = deliverable
            append_artifact_failures(message, failures)
            payload["text"] = message.text
            if uploads:
                payload["artifacts"] = manifest
                data = {"payload": json.dumps(payload, ensure_ascii=False)}
                files = uploads
        if not message.text.strip() and not message.artifacts:
            return
        headers = {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else None
        if self.client is not None:
            await self._post_with_artifact_fallback(
                self.client,
                message=message,
                payload=payload,
                headers=headers,
                data=data,
                files=files,
            )
            return
        async with httpx.AsyncClient() as client:
            await self._post_with_artifact_fallback(
                client,
                message=message,
                payload=payload,
                headers=headers,
                data=data,
                files=files,
            )

    async def _post_with_artifact_fallback(
        self,
        client: httpx.AsyncClient,
        *,
        message,
        payload: dict,
        headers: dict[str, str] | None,
        data: dict[str, str] | None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None,
    ) -> None:
        try:
            await self._post_with_retries(
                client,
                payload=payload,
                headers=headers,
                data=data,
                files=files,
            )
            return
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if files is None or status not in {400, 413, 415, 422}:
                raise

        rejected = list(message.artifacts)
        message.artifacts = []
        append_artifact_failures(
            message,
            [
                f"{artifact.filename}: webhook rejected the attachment (HTTP {status})"
                for artifact in rejected
            ],
        )
        if not message.text.strip():
            return
        payload.pop("artifacts", None)
        payload["text"] = message.text
        await self._post_with_retries(
            client,
            payload=payload,
            headers=headers,
        )

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        *,
        payload: dict,
        headers: dict[str, str] | None,
        data: dict[str, str] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    ) -> None:
        delivery_id = str(payload.get("metadata", {}).get("delivery_id") or "")
        attempts = self.max_attempts if delivery_id else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(
                    self.outbound_url,
                    json=payload if files is None else None,
                    data=data,
                    files=files,
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

    def can_deliver(self, channel_id: str) -> bool:
        if channel_id in self.channel_sinks:
            return True
        if channel_id in BUILTIN_CHANNEL_IDS:
            return False
        return self.default_sink is not None

    def prepare_durable_message(self, message) -> None:
        sink = self.channel_sinks.get(message.channel_id)
        if sink is None and message.channel_id in BUILTIN_CHANNEL_IDS:
            return
        sink = sink or self.default_sink
        if sink is None:
            return
        if message.artifacts and not bool(
            getattr(sink, "supports_outbound_artifacts", False)
        ):
            count = len(message.artifacts)
            notice = (
                f"Attachment delivery unavailable: this channel does not support "
                f"{count} staged artifact{'s' if count != 1 else ''}."
            )
            message.text = "\n\n".join(part for part in (message.text, notice) if part)
            message.artifacts = []
        prepare = getattr(sink, "prepare_durable_message", None)
        if callable(prepare):
            prepare(message)

    async def send_message(self, message) -> None:
        sink = self._sink_for(message.channel_id)
        await sink.send_message(message)

    def _sink_for(self, channel_id: str):
        sink = self.channel_sinks.get(channel_id)
        if sink is None and channel_id in BUILTIN_CHANNEL_IDS:
            raise RuntimeError(
                f"Built-in channel {channel_id!r} is not enabled; refusing fallback delivery."
            )
        sink = sink or self.default_sink
        if sink is None:
            raise RuntimeError(f"No outbound sink is configured for channel {channel_id!r}.")
        return sink

from __future__ import annotations

import asyncio
from collections import defaultdict
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import httpx
import websockets

from ..config import validate_http_endpoint
from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .base import BaseChannelAdapter
from .media import materialize_inbound_images
from .qq_media import QQImageReference, QQMediaMaterializer, parse_qq_image_references


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_GUILD_MEMBERS = 1 << 1
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
SUPPORTED_EVENTS = {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}
MENTION_PREFIX_PATTERN = re.compile(r"^(?:<@!?\w+>\s*)+")
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
INBOUND_QUEUE_LIMIT = 64
GROUP_PASSIVE_REPLY_MAX_AGE_S = 270.0
C2C_PASSIVE_REPLY_MAX_AGE_S = 3300.0


def _config_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class QQChannelAdapter(BaseChannelAdapter):
    channel_id = "qq"

    def __init__(
        self,
        *,
        enabled: bool,
        app_id: str,
        client_secret: str,
        middleware,
        api_base: str = DEFAULT_API_BASE,
        token_url: str = TOKEN_URL,
        http_client: httpx.AsyncClient | None = None,
        media_dir: Path | None = None,
        media_materializer: QQMediaMaterializer | None = None,
        media_cleanup_sleep=asyncio.sleep,
        websocket_factory=websockets.connect,
        sleep=asyncio.sleep,
        clock=time.time,
        startup_timeout_s: float = 15.0,
        markdown_enabled: bool = True,
        access_policy: ChannelAccessPolicy | None = None,
    ) -> None:
        super().__init__(middleware=middleware, access_policy=access_policy)
        self.enabled = enabled
        self.app_id = app_id.strip()
        self.client_secret = client_secret.strip()
        self.api_base = api_base.strip().rstrip("/")
        self.token_url = token_url
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
        self.media_materializer = media_materializer or QQMediaMaterializer(
            root=media_dir or Path(".imcodex") / "channels" / "qq" / "inbound-media",
            http_client=self.http_client,
            clock=clock,
            cleanup_sleep=media_cleanup_sleep,
        )
        self.websocket_factory = websocket_factory
        self.sleep = sleep
        self.clock = clock
        self.startup_timeout_s = startup_timeout_s
        self.markdown_enabled = markdown_enabled
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._msg_seq: dict[str, int] = defaultdict(int)
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._session_epoch = 0
        self._inbound_queue: asyncio.Queue[
            tuple[InboundMessage, tuple[QQImageReference, ...], int | None, int]
        ] = asyncio.Queue(
            maxsize=INBOUND_QUEUE_LIMIT
        )
        self._queued_message_ids: set[tuple[str, str]] = set()
        self._inbound_worker_task: asyncio.Task[None] | None = None

    @classmethod
    def from_config(cls, *, config: dict[str, object], middleware):
        return cls(
            enabled=bool(config.get("enabled")),
            app_id=str(config.get("app_id") or ""),
            client_secret=str(config.get("client_secret") or ""),
            middleware=middleware,
            api_base=str(config.get("api_base") or DEFAULT_API_BASE),
            markdown_enabled=_config_bool(config.get("markdown_enabled")),
            media_dir=Path(str(config.get("media_dir") or ".imcodex/channels/qq/inbound-media")),
            access_policy=ChannelAccessPolicy.from_config(config),
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        self.validate_startup_configuration()
        await self.media_materializer.start()
        self._stop_event.clear()
        self._ready_event.clear()
        self._ensure_inbound_worker()
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_forever())
        mark_channel_health(
            "qq",
            enabled=True,
            connected=False,
            status="connecting",
            **self.access_policy_health(),
        )

    def validate_startup_configuration(self) -> None:
        if not self.enabled:
            return
        if not self.app_id or not self.client_secret:
            raise RuntimeError("QQ adapter requires app_id and client_secret when enabled.")
        validate_http_endpoint(self.api_base, key="IMCODEX_QQ_API_BASE")

    async def stop(self) -> None:
        self._stop_event.set()
        self._ready_event.set()
        if self._runner_task is not None:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None
        if self._inbound_worker_task is not None:
            self._inbound_worker_task.cancel()
            try:
                await self._inbound_worker_task
            except asyncio.CancelledError:
                pass
            self._inbound_worker_task = None
        await self.media_materializer.stop()
        self._drain_inbound_queue()
        if self._owns_http_client:
            await self.http_client.aclose()

    def parse_inbound_event(self, event_type: str, payload: dict[str, Any]) -> InboundMessage | None:
        parsed = self._parse_inbound_event(event_type, payload)
        return parsed[0] if parsed is not None else None

    def _parse_inbound_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[InboundMessage, tuple[QQImageReference, ...]] | None:
        if event_type not in SUPPORTED_EVENTS:
            return None
        author = payload.get("author") or {}
        text = (payload.get("content") or "").strip()
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            text = MENTION_PREFIX_PATTERN.sub("", text).strip()
        image_references = parse_qq_image_references(payload.get("attachments"))
        if not text and not image_references:
            return None
        if event_type == "C2C_MESSAGE_CREATE":
            sender = author.get("user_openid") or author.get("id")
            conversation_id = f"c2c:{sender}" if sender else ""
        else:
            sender = author.get("member_openid") or author.get("id")
            group_openid = payload.get("group_openid") or ""
            conversation_id = f"group:{group_openid}" if group_openid else ""
        message_id = str(payload.get("id") or "")
        if not sender or not conversation_id or not message_id:
            return None
        return (
            InboundMessage(
                channel_id="qq",
                conversation_id=conversation_id,
                user_id=str(sender),
                message_id=message_id,
                text=text,
            ),
            image_references,
        )

    async def handle_dispatch_event(self, event_type: str, payload: dict[str, Any]) -> None:
        parsed = self._parse_inbound_event(event_type, payload)
        if parsed is None:
            return
        inbound, image_references = parsed
        if not self.inbound_allowed(inbound):
            suppressed = self.prepare_access_denial_report()
            if suppressed is not None:
                self.emit_access_denial(inbound, suppressed)
            return
        prepare_inbound = None
        if image_references:
            prepare_inbound = lambda message: self._materialize_inbound(
                message,
                image_references,
            )
        await self.dispatch_inbound(
            inbound,
            reply_to_message_id=inbound.message_id,
            prepare_inbound=prepare_inbound,
            pending_attachment_count=len(image_references),
        )

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != "qq" or not message.text.strip():
            return
        self.ensure_outbound_allowed(message)
        token = await self._get_access_token()
        path = self._conversation_path(message.conversation_id)
        reply_to = self._reply_to_message_id(message)
        sequence_key = reply_to or message.conversation_id
        body = self._message_body(
            message.text,
            msg_seq=self._message_sequence(message, sequence_key),
            reply_to=reply_to,
        )
        try:
            await self._post_message(path=path, token=token, body=body)
        except httpx.HTTPStatusError as exc:
            if self._acknowledge_duplicate_delivery(message, exc):
                return
            if not self._should_retry_plain_text(exc):
                raise
            logger.warning("QQ markdown message failed; retrying as plain text", exc_info=True)
            fallback_body = self._message_body(
                message.text,
                msg_seq=body["msg_seq"],
                reply_to=reply_to,
                markdown_enabled=False,
            )
            try:
                await self._post_message(path=path, token=token, body=fallback_body)
            except httpx.HTTPStatusError as fallback_exc:
                if self._acknowledge_duplicate_delivery(message, fallback_exc):
                    return
                raise

    def prepare_durable_message(self, message: OutboundMessage) -> None:
        if message.channel_id != "qq" or message.metadata.get("qq_reply_identity_pinned"):
            return
        reply_to = self._reply_to_message_id(message)
        message.metadata["qq_reply_identity_pinned"] = True
        message.metadata["qq_reply_to_message_id"] = reply_to

    async def _run_forever(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            reconnect_delay = RECONNECT_INITIAL_DELAY_S
            try:
                logger.info("QQ adapter connecting via %s", self.api_base)
                emit_event(
                    component="channels.qq",
                    event="qq.gateway.connecting",
                    message="QQ adapter connecting to gateway",
                    data={"api_base": self.api_base},
                )
                token = await self._get_access_token()
                gateway = await self._get_gateway_url(token)
                await self._run_session(gateway, token)
                failures = 0
                self._mark_disconnected(status="reconnecting")
                if not self._stop_event.is_set():
                    emit_event(
                        component="channels.qq",
                        event="qq.gateway.disconnected",
                        message="QQ gateway session ended; reconnecting",
                        data={"retry_delay_s": reconnect_delay},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                reconnect_delay = self._reconnect_delay(failures)
                self._ready_event.clear()
                self._mark_disconnected(
                    status="reconnecting",
                    error_type=type(exc).__name__,
                    retry_delay_s=reconnect_delay,
                )
                logger.warning(
                    "QQ adapter connection failed; retrying in %.1fs: %s",
                    reconnect_delay,
                    exc,
                )
                logger.debug("QQ adapter connection failure details", exc_info=True)
                emit_event(
                    component="channels.qq",
                    event="qq.gateway.connect_failed",
                    level="ERROR",
                    message="QQ adapter gateway connection failed; retrying",
                    data={
                        "api_base": self.api_base,
                        "error_type": type(exc).__name__,
                        "retry_attempt": failures,
                        "retry_delay_s": reconnect_delay,
                    },
                )
            if not self._stop_event.is_set():
                await self.sleep(reconnect_delay)

    async def _run_session(self, gateway_url: str, token: str) -> None:
        heartbeat_task: asyncio.Task | None = None
        try:
            async with self.websocket_factory(gateway_url) as websocket:
                async for raw in websocket:
                    payload = json.loads(raw)
                    seq = payload.get("s")
                    sequence = seq if isinstance(seq, int) else None
                    op = payload.get("op")
                    if op == OP_HELLO:
                        interval_ms = (payload.get("d") or {}).get("heartbeat_interval", 45000)
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket, interval_ms / 1000.0))
                        await websocket.send(json.dumps(self._resume_or_identify_payload(token)))
                        continue
                    if op == OP_DISPATCH:
                        event_type = payload.get("t")
                        data = payload.get("d") or {}
                        if event_type == "READY":
                            session_id = str(data.get("session_id") or "") or None
                            if self._session_id is not None and self._session_id != session_id:
                                self._session_epoch += 1
                            self._session_id = session_id
                            self._advance_sequence_if_idle(sequence)
                            logger.info("QQ gateway ready session_id=%s", self._session_id)
                            emit_event(
                                component="channels.qq",
                                event="qq.gateway.ready",
                                message="QQ gateway ready",
                                data={"session_id": self._session_id},
                            )
                            mark_channel_health(
                                "qq",
                                connected=True,
                                session_id=self._session_id,
                                status="connected",
                            )
                            self._ready_event.set()
                            continue
                        if event_type == "RESUMED":
                            self._advance_sequence_if_idle(sequence)
                            logger.info("QQ gateway resumed")
                            emit_event(
                                component="channels.qq",
                                event="qq.gateway.resumed",
                                message="QQ gateway resumed",
                            )
                            mark_channel_health(
                                "qq",
                                connected=True,
                                session_id=self._session_id,
                                status="connected",
                            )
                            self._ready_event.set()
                            continue
                        if event_type in SUPPORTED_EVENTS:
                            self._queue_dispatch_event(event_type, data, sequence)
                            continue
                        self._advance_sequence_if_idle(sequence)
                    if op == OP_HEARTBEAT_ACK:
                        continue
                    if op == OP_INVALID_SESSION:
                        self._session_id = None
                        self._last_seq = None
                        self._session_epoch += 1
                        break
                    if op == OP_RECONNECT:
                        break
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    def _queue_dispatch_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        sequence: int | None,
    ) -> None:
        parsed = self._parse_inbound_event(event_type, payload)
        if parsed is None:
            self._advance_sequence_if_idle(sequence)
            return
        inbound, image_references = parsed
        if not self.inbound_allowed(inbound):
            suppressed = self.prepare_access_denial_report()
            if suppressed is not None:
                self.emit_access_denial(inbound, suppressed)
            self._advance_sequence_if_idle(sequence)
            return
        message_key = (inbound.conversation_id, inbound.message_id)
        if message_key in self._queued_message_ids:
            return
        self._ensure_inbound_worker()
        try:
            self._inbound_queue.put_nowait(
                (inbound, image_references, sequence, self._session_epoch)
            )
        except asyncio.QueueFull:
            emit_event(
                component="channels.qq",
                event="message.inbound.queue_overflow",
                level="ERROR",
                message="QQ inbound queue is full; reconnecting for replay",
                data={"queue_limit": INBOUND_QUEUE_LIMIT},
            )
            raise RuntimeError("QQ inbound queue is full; reconnecting before acknowledging messages") from None
        self._queued_message_ids.add(message_key)

    def _ensure_inbound_worker(self) -> None:
        if self._inbound_worker_task is None or self._inbound_worker_task.done():
            self._inbound_worker_task = asyncio.create_task(self._run_inbound_worker())

    async def _run_inbound_worker(self) -> None:
        while True:
            inbound, image_references, sequence, epoch = await self._inbound_queue.get()
            message_key = (inbound.conversation_id, inbound.message_id)
            failures = 0
            try:
                prepare_inbound = None
                if image_references:
                    prepare_inbound = lambda message: self._materialize_inbound(
                        message,
                        image_references,
                    )
                while not self._stop_event.is_set():
                    try:
                        await self.dispatch_inbound(
                            inbound,
                            reply_to_message_id=inbound.message_id,
                            prepare_inbound=prepare_inbound,
                            pending_attachment_count=len(image_references),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failures += 1
                        delay = self._reconnect_delay(failures)
                        logger.warning(
                            "QQ inbound delivery failed; retrying in %.1fs: %s",
                            delay,
                            type(exc).__name__,
                        )
                        emit_event(
                            component="channels.qq",
                            event="message.inbound.delivery_failed",
                            level="ERROR",
                            message="QQ inbound delivery failed; retrying",
                            channel_id=self.channel_id,
                            conversation_id=inbound.conversation_id,
                            user_id=inbound.user_id,
                            message_id=inbound.message_id,
                            data={
                                "error_type": type(exc).__name__,
                                "retry_attempt": failures,
                                "retry_delay_s": delay,
                            },
                        )
                        await self.sleep(delay)
                        continue
                    self._advance_sequence(sequence, epoch=epoch)
                    break
            finally:
                self._queued_message_ids.discard(message_key)
                self._inbound_queue.task_done()

    async def _materialize_inbound(
        self,
        inbound: InboundMessage,
        image_references: tuple[QQImageReference, ...],
    ) -> InboundMessage:
        return await materialize_inbound_images(
            inbound,
            image_references,
            self.media_materializer,
        )

    def _advance_sequence_if_idle(self, sequence: int | None) -> None:
        if self._inbound_queue.empty() and not self._queued_message_ids:
            self._advance_sequence(sequence, epoch=self._session_epoch)

    def _advance_sequence(self, sequence: int | None, *, epoch: int) -> None:
        if sequence is None or epoch != self._session_epoch:
            return
        self._last_seq = max(self._last_seq or sequence, sequence)

    def _drain_inbound_queue(self) -> None:
        while True:
            try:
                inbound, _image_references, _sequence, _epoch = self._inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_message_ids.discard((inbound.conversation_id, inbound.message_id))
            self._inbound_queue.task_done()

    def _reconnect_delay(self, failures: int) -> float:
        if failures <= 0:
            return RECONNECT_INITIAL_DELAY_S
        return min(RECONNECT_MAX_DELAY_S, RECONNECT_INITIAL_DELAY_S * (2 ** (failures - 1)))

    def _mark_disconnected(self, *, status: str, **changes: Any) -> None:
        payload = {
            "connected": False,
            "session_id": self._session_id,
            "status": status,
        }
        payload.update(changes)
        mark_channel_health("qq", **payload)

    async def _heartbeat_loop(self, websocket, interval_seconds: float) -> None:
        while not self._stop_event.is_set():
            await self.sleep(interval_seconds)
            await websocket.send(json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq}))

    def _resume_or_identify_payload(self, token: str) -> dict[str, Any]:
        if self._session_id and self._last_seq is not None:
            return {
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            }
        return {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": (
                    INTENT_PUBLIC_GUILD_MESSAGES | INTENT_GUILD_MEMBERS | INTENT_DIRECT_MESSAGE | INTENT_GROUP_AND_C2C
                ),
                "shard": [0, 1],
            },
        }

    async def _get_access_token(self) -> str:
        if self._access_token and self.clock() < self._access_token_expires_at - 60:
            return self._access_token
        response = await self.http_client.post(
            self.token_url,
            headers={"Content-Type": "application/json"},
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("QQ token response was not an object.")
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("QQ token response missing required access_token field.")
        expires_in = int(payload.get("expires_in", 7200))
        self._access_token = token
        self._access_token_expires_at = self.clock() + expires_in
        return token

    async def _get_gateway_url(self, token: str) -> str:
        payload = None
        last_error: Exception | None = None
        for api_base in self._gateway_candidates():
            response = await self.http_client.get(
                f"{api_base}/gateway",
                headers={
                    "Authorization": f"QQBot {token}",
                    "Content-Type": "application/json",
                },
            )
            if response.is_success:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("QQ gateway response was not an object.")
                url = payload.get("url")
                if not url:
                    raise RuntimeError("QQ gateway response missing required url field.")
                self.api_base = api_base
                return str(url)
            last_error = httpx.HTTPStatusError(
                f"QQ gateway request failed with status {response.status_code}",
                request=response.request,
                response=response,
            )
            if not self._should_fallback_to_sandbox(response):
                response.raise_for_status()
        if last_error is not None:
            raise last_error
        raise RuntimeError("QQ gateway request did not return a response")

    def _gateway_candidates(self) -> list[str]:
        if self.api_base == DEFAULT_API_BASE:
            return [DEFAULT_API_BASE, SANDBOX_API_BASE]
        return [self.api_base]

    def _should_fallback_to_sandbox(self, response: httpx.Response) -> bool:
        if response.status_code != 401 or self.api_base != DEFAULT_API_BASE:
            return False
        try:
            payload = response.json()
        except Exception:
            return False
        message = str(payload.get("message") or "")
        err_code = str(payload.get("err_code") or payload.get("code") or "")
        return "白名单" in message or err_code == "40023002"

    def _conversation_path(self, conversation_id: str) -> str:
        if conversation_id.startswith("c2c:"):
            return f"/v2/users/{conversation_id[4:]}/messages"
        if conversation_id.startswith("group:"):
            return f"/v2/groups/{conversation_id[6:]}/messages"
        raise ValueError(f"Unsupported QQ conversation id: {conversation_id}")

    def _conversation_user_id(self, conversation_id: str) -> str | None:
        if conversation_id.startswith("c2c:"):
            return conversation_id[4:] or None
        return None

    def _next_msg_seq(self, conversation_id: str) -> int:
        self._msg_seq[conversation_id] += 1
        return self._msg_seq[conversation_id]

    def _message_sequence(self, message: OutboundMessage, sequence_key: str) -> int:
        delivery_id = str(message.metadata.get("delivery_id") or "").strip()
        if not delivery_id:
            return self._next_msg_seq(sequence_key)
        digest = hashlib.sha256(delivery_id.encode("utf-8")).digest()
        # QQ uses msg_id + msg_seq as its retry identity. Keep the sequence
        # stable across bridge processes while remaining a positive signed int.
        return int.from_bytes(digest[:4], "big") % 2_147_483_647 + 1

    def _message_body(
        self,
        text: str,
        *,
        msg_seq: int,
        reply_to: str | None,
        markdown_enabled: bool | None = None,
    ) -> dict[str, Any]:
        use_markdown = self.markdown_enabled if markdown_enabled is None else markdown_enabled
        if use_markdown:
            body: dict[str, Any] = {
                "markdown": {"content": text},
                "msg_type": 2,
                "msg_seq": msg_seq,
            }
        else:
            body = {
                "content": text,
                "msg_type": 0,
                "msg_seq": msg_seq,
            }
        if reply_to:
            body["msg_id"] = reply_to
        return body

    async def _post_message(self, *, path: str, token: str, body: dict[str, Any]) -> None:
        response = await self.http_client.post(
            f"{self.api_base}{path}",
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()

    def _should_retry_plain_text(self, exc: httpx.HTTPStatusError) -> bool:
        return self.markdown_enabled and exc.response.status_code in {400, 403}

    @staticmethod
    def _is_duplicate_message_error(exc: httpx.HTTPStatusError) -> bool:
        try:
            payload = exc.response.json()
        except (TypeError, ValueError):
            payload = {}
        message = str(payload.get("message") or payload.get("msg") or "")
        return "消息被去重" in message or "duplicate" in message.lower()

    def _acknowledge_duplicate_delivery(
        self,
        message: OutboundMessage,
        exc: httpx.HTTPStatusError,
    ) -> bool:
        if not message.metadata.get("delivery_id") or not self._is_duplicate_message_error(
            exc
        ):
            return False
        emit_event(
            component="channels.qq",
            event="qq.delivery.duplicate_acknowledged",
            message="QQ confirmed that this stable delivery was already accepted",
            channel_id="qq",
            conversation_id=message.conversation_id,
            data={"delivery_id": message.metadata.get("delivery_id")},
        )
        return True

    def _reply_to_message_id(self, message: OutboundMessage) -> str | None:
        conversation_id = message.conversation_id
        if message.metadata.get("qq_reply_identity_pinned"):
            pinned = str(message.metadata.get("qq_reply_to_message_id") or "").strip()
            return pinned or None
        message_id = message.metadata.get("reply_to_message_id") or message.metadata.get(
            "message_id"
        )
        seen_at = message.metadata.get("reply_to_seen_at")
        if not message_id:
            context = self._route_context(message)
            if context is not None:
                message_id = context.last_inbound_message_id
                seen_at = context.last_inbound_seen_at
        elif seen_at is None:
            context = self._route_context(message)
            if context is not None and context.last_inbound_message_id == str(message_id):
                seen_at = context.last_inbound_seen_at
        if not message_id:
            return None
        try:
            seen_at = float(seen_at)
        except (TypeError, ValueError):
            emit_event(
                component="channels.qq",
                event="qq.passive_reply.unverified",
                message="QQ passive reply age is unknown; using proactive delivery",
                channel_id="qq",
                conversation_id=conversation_id,
            )
            return None
        max_age_s = (
            GROUP_PASSIVE_REPLY_MAX_AGE_S
            if conversation_id.startswith("group:")
            else C2C_PASSIVE_REPLY_MAX_AGE_S
        )
        age_s = max(0.0, self.clock() - seen_at)
        if age_s > max_age_s:
            emit_event(
                component="channels.qq",
                event="qq.passive_reply.expired",
                message="QQ passive reply window expired; using proactive delivery",
                channel_id="qq",
                conversation_id=conversation_id,
                data={"age_s": round(age_s, 3), "max_age_s": max_age_s},
            )
            return None
        return str(message_id)

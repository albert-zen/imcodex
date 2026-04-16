from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from typing import Any

import httpx
import websockets

from ..models import InboundMessage, OutboundMessage


logger = logging.getLogger(__name__)
GENERIC_USER_ERROR_TEXT = "Request failed while talking to Codex. Please try again."

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


class QQChannelAdapter:
    def __init__(
        self,
        *,
        enabled: bool,
        app_id: str,
        client_secret: str,
        service,
        api_base: str = DEFAULT_API_BASE,
        token_url: str = TOKEN_URL,
        http_client: httpx.AsyncClient | None = None,
        websocket_factory=websockets.connect,
        sleep=asyncio.sleep,
        clock=time.time,
        startup_timeout_s: float = 15.0,
    ) -> None:
        self.enabled = enabled
        self.app_id = app_id
        self.client_secret = client_secret
        self.service = service
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
        self.websocket_factory = websocket_factory
        self.sleep = sleep
        self.clock = clock
        self.startup_timeout_s = startup_timeout_s
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._msg_seq: dict[str, int] = defaultdict(int)
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._session_id: str | None = None
        self._last_seq: int | None = None

    async def start(self) -> None:
        if not self.enabled:
            return
        if not self.app_id or not self.client_secret:
            raise RuntimeError("QQ adapter requires app_id and client_secret when enabled.")
        self._stop_event.clear()
        self._ready_event.clear()
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_forever())
        await asyncio.wait_for(self._ready_event.wait(), timeout=self.startup_timeout_s)

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
        if self._owns_http_client:
            await self.http_client.aclose()

    def parse_inbound_event(self, event_type: str, payload: dict[str, Any]) -> InboundMessage | None:
        if event_type not in SUPPORTED_EVENTS:
            return None
        author = payload.get("author") or {}
        text = (payload.get("content") or "").strip()
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            text = MENTION_PREFIX_PATTERN.sub("", text).strip()
        if not text:
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
        return InboundMessage(
            channel_id="qq",
            conversation_id=conversation_id,
            user_id=str(sender),
            message_id=message_id,
            text=text,
        )

    async def handle_dispatch_event(self, event_type: str, payload: dict[str, Any]) -> None:
        inbound = self.parse_inbound_event(event_type, payload)
        if inbound is None:
            return
        self._note_inbound_message(inbound.conversation_id, inbound.message_id)
        try:
            outbound = await self.service.handle_inbound(inbound)
            for message in outbound:
                if message.channel_id != "qq":
                    continue
                message.metadata.setdefault("reply_to_message_id", inbound.message_id)
                await self.send_message(message)
        except Exception as exc:
            logger.exception("QQ inbound handling failed")
            await self.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id=inbound.conversation_id,
                    message_type="error",
                    text=GENERIC_USER_ERROR_TEXT,
                    metadata={"reply_to_message_id": inbound.message_id},
                )
            )

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != "qq" or not message.text.strip():
            return
        token = await self._get_access_token()
        path = self._conversation_path(message.conversation_id)
        reply_to = (
            message.metadata.get("reply_to_message_id")
            or message.metadata.get("message_id")
            or self._reply_to_message_id(message.conversation_id)
        )
        sequence_key = reply_to or message.conversation_id
        body = {
            "content": message.text,
            "msg_type": 0,
            "msg_seq": self._next_msg_seq(sequence_key),
        }
        if reply_to:
            body["msg_id"] = reply_to
        response = await self.http_client.post(
            f"{self.api_base}{path}",
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                logger.info("QQ adapter connecting via %s", self.api_base)
                token = await self._get_access_token()
                gateway = await self._get_gateway_url(token)
                await self._run_session(gateway, token)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("QQ adapter loop failed")
            if not self._stop_event.is_set():
                await self.sleep(1)

    async def _run_session(self, gateway_url: str, token: str) -> None:
        heartbeat_task: asyncio.Task | None = None
        try:
            async with self.websocket_factory(gateway_url) as websocket:
                async for raw in websocket:
                    payload = json.loads(raw)
                    seq = payload.get("s")
                    if isinstance(seq, int):
                        self._last_seq = seq
                    op = payload.get("op")
                    if op == OP_HELLO:
                        interval_ms = (payload.get("d") or {}).get("heartbeat_interval", 45000)
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(websocket, interval_ms / 1000.0)
                        )
                        await websocket.send(json.dumps(self._resume_or_identify_payload(token)))
                        continue
                    if op == OP_DISPATCH:
                        event_type = payload.get("t")
                        data = payload.get("d") or {}
                        if event_type == "READY":
                            self._session_id = data.get("session_id")
                            logger.info("QQ gateway ready session_id=%s", self._session_id)
                            self._ready_event.set()
                            continue
                        if event_type == "RESUMED":
                            logger.info("QQ gateway resumed")
                            self._ready_event.set()
                            continue
                        if event_type in SUPPORTED_EVENTS:
                            await self.handle_dispatch_event(event_type, data)
                            continue
                    if op == OP_HEARTBEAT_ACK:
                        continue
                    if op == OP_INVALID_SESSION:
                        self._session_id = None
                        self._last_seq = None
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
                    INTENT_PUBLIC_GUILD_MESSAGES
                    | INTENT_GUILD_MEMBERS
                    | INTENT_DIRECT_MESSAGE
                    | INTENT_GROUP_AND_C2C
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
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"QQ token response missing access_token: {payload}")
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
                url = payload.get("url")
                if not url:
                    raise RuntimeError(f"QQ gateway response missing url: {payload}")
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

    def _next_msg_seq(self, conversation_id: str) -> int:
        self._msg_seq[conversation_id] += 1
        return self._msg_seq[conversation_id]

    def _note_inbound_message(self, conversation_id: str, message_id: str) -> None:
        store = getattr(self.service, "store", None)
        if store is None:
            return
        note = getattr(store, "note_inbound_message", None)
        if callable(note):
            note("qq", conversation_id, message_id)

    def _reply_to_message_id(self, conversation_id: str) -> str | None:
        store = getattr(self.service, "store", None)
        if store is None:
            return None
        get_binding = getattr(store, "get_binding", None)
        if not callable(get_binding):
            return None
        binding = get_binding("qq", conversation_id)
        reply_context = getattr(binding, "reply_context", None) or {}
        return reply_context.get("last_inbound_message_id") or getattr(binding, "last_inbound_message_id", None)

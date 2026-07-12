from __future__ import annotations

import asyncio
from functools import partial
import logging
import re
from threading import BoundedSemaphore, Lock
from typing import Callable

from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .base import BaseChannelAdapter
from .text import split_text


logger = logging.getLogger(__name__)

FEISHU_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"
FEISHU_TEXT_LIMIT = 3500
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
INBOUND_QUEUE_LIMIT = 64
SDK_DISCONNECT_TIMEOUT_S = 5.0
SDK_JOIN_TIMEOUT_S = 2.0
CONVERSATION_PATTERN = re.compile(r"^chat:([^:]+)(?::thread:(.+))?$")


def _config_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class FeishuChannelAdapter(BaseChannelAdapter):
    """Text-only Feishu/Lark adapter over the official Channel SDK."""

    channel_id = "feishu"

    def __init__(
        self,
        *,
        enabled: bool,
        app_id: str,
        app_secret: str,
        middleware,
        domain: str = "feishu",
        access_policy: ChannelAccessPolicy | None = None,
        require_mention: bool = True,
        startup_timeout_s: float = 30.0,
        channel_factory: Callable[..., object] | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        super().__init__(
            middleware=middleware,
            access_policy=access_policy or ChannelAccessPolicy(allowed_user_ids=frozenset()),
        )
        self.enabled = enabled
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.domain = self._normalize_domain(domain)
        self.require_mention = require_mention
        self.startup_timeout_s = max(1.0, float(startup_timeout_s))
        self.channel_factory = channel_factory
        self.sleep = sleep
        self._sdk: object | None = None
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: list[Callable[[], object]] = []
        self._inbound_queue: asyncio.Queue[InboundMessage] | None = None
        self._inbound_worker_task: asyncio.Task | None = None
        self._inbound_slots = BoundedSemaphore(INBOUND_QUEUE_LIMIT)
        self._overflow_lock = Lock()
        self._overflow_count = 0
        self._last_overflow_report_at = 0.0

    @classmethod
    def from_config(cls, *, config: dict[str, object], middleware):
        return cls(
            enabled=bool(config.get("enabled")),
            app_id=str(config.get("app_id") or ""),
            app_secret=str(config.get("app_secret") or ""),
            middleware=middleware,
            domain=str(config.get("domain") or "feishu"),
            access_policy=ChannelAccessPolicy.from_config(config),
            require_mention=_config_bool(config.get("require_mention"), True),
            startup_timeout_s=float(config.get("startup_timeout_s") or 30.0),
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "Feishu adapter requires IMCODEX_FEISHU_APP_ID and IMCODEX_FEISHU_APP_SECRET when enabled."
            )
        if not self.access_policy.has_allowed_users:
            logger.warning(
                "Feishu has no allowed user IDs; inbound messages will be denied. Set IMCODEX_FEISHU_ALLOWED_USER_IDS."
            )
        self._main_loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._inbound_queue = asyncio.Queue(maxsize=INBOUND_QUEUE_LIMIT)
        self._inbound_slots = BoundedSemaphore(INBOUND_QUEUE_LIMIT)
        self._sdk = self._create_sdk()
        try:
            self._subscribe_sdk(self._sdk)
            self._inbound_worker_task = asyncio.create_task(self._run_inbound_worker())
            if self._runner_task is None or self._runner_task.done():
                self._runner_task = asyncio.create_task(self._run_forever())
        except BaseException:
            await self._detach_sdk()
            raise
        mark_channel_health("feishu", enabled=True, connected=False, status="connecting")

    async def stop(self) -> None:
        errors: list[Exception] = []
        self._stop_event.set()
        self._clear_subscriptions()
        if self._inbound_worker_task is not None:
            self._inbound_worker_task.cancel()
            try:
                await self._inbound_worker_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(exc)
            self._inbound_worker_task = None
        self._drain_inbound_queue()
        if self._runner_task is not None:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(exc)
            self._runner_task = None
        try:
            await self._detach_sdk()
        except Exception as exc:
            errors.append(exc)
        self._main_loop = None
        mark_channel_health("feishu", connected=False, status="stopped")
        if errors:
            raise ExceptionGroup("Feishu shutdown failed", errors)

    def parse_inbound_message(self, message: object) -> InboundMessage | None:
        if str(getattr(message, "raw_content_type", "")) != "text":
            return None
        conversation = getattr(message, "conversation", None)
        sender = getattr(message, "sender", None)
        chat_id = str(getattr(conversation, "chat_id", "") or "")
        chat_type = str(getattr(conversation, "chat_type", "") or "")
        thread_id = str(getattr(conversation, "thread_id", "") or "")
        user_id = str(getattr(sender, "open_id", "") or "")
        message_id = str(getattr(message, "message_id", "") or getattr(message, "id", "") or "")
        text = str(getattr(message, "content_text", "") or "").strip()
        if not chat_id or not user_id or not message_id or not text:
            return None
        if chat_type in {"group", "topic"}:
            if self.require_mention and not bool(getattr(message, "mentioned_bot", False)):
                return None
            text = self._strip_bot_mention(text)
            if not text:
                return None
        conversation_id = f"chat:{chat_id}"
        if thread_id:
            conversation_id += f":thread:{thread_id}"
        return InboundMessage(
            channel_id=self.channel_id,
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message_id,
            text=text,
        )

    async def handle_sdk_message(self, message: object) -> None:
        inbound = self.parse_inbound_message(message)
        if inbound is None:
            return
        await self.dispatch_inbound(inbound, reply_to_message_id=inbound.message_id)

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != self.channel_id or not message.text.strip():
            return
        self.ensure_outbound_allowed(message)
        sdk = self._sdk
        if sdk is None:
            raise RuntimeError("Feishu channel is not connected.")
        chat_id, thread_id = self._parse_conversation_id(message.conversation_id)
        reply_to = str(
            message.metadata.get("reply_to_message_id")
            or message.metadata.get("message_id")
            or self._last_inbound_message_id(message)
            or ""
        )
        if thread_id and not reply_to:
            raise RuntimeError("Feishu topic delivery requires a persisted inbound message ID for reply routing.")
        for chunk in split_text(message.text, limit=FEISHU_TEXT_LIMIT):
            opts: dict[str, object] = {"receive_id_type": "chat_id"}
            if reply_to:
                opts["reply_to"] = reply_to
            if thread_id:
                opts["reply_in_thread"] = True
            result = await sdk.send(chat_id, {"text": chunk}, opts)
            if hasattr(result, "success") and not bool(getattr(result, "success")):
                raise RuntimeError("Feishu rejected an outbound message.")

    async def _run_forever(self) -> None:
        failures = 0
        try:
            while not self._stop_event.is_set():
                sdk = self._sdk
                try:
                    if sdk is None:
                        sdk = self._create_sdk()
                        self._sdk = sdk
                        self._subscribe_sdk(sdk)
                    await sdk.connect_until_ready(timeout=self.startup_timeout_s)
                    failures = 0
                    snapshot = self._connection_snapshot(sdk)
                    mark_channel_health(
                        "feishu",
                        connected=True,
                        status="connected",
                        connection_state=snapshot.get("state"),
                    )
                    emit_event(
                        component="channels.feishu",
                        event="feishu.websocket.ready",
                        message="Feishu/Lark websocket is ready",
                        data={"domain": self.domain},
                    )
                    await self._stop_event.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    delay = self._reconnect_delay(failures)
                    logger.warning(
                        "Feishu connection failed; retrying in %.1fs: %s",
                        delay,
                        type(exc).__name__,
                    )
                    mark_channel_health(
                        "feishu",
                        connected=False,
                        status="reconnecting",
                        error_type=type(exc).__name__,
                        retry_delay_s=delay,
                    )
                    emit_event(
                        component="channels.feishu",
                        event="feishu.websocket.failed",
                        level="ERROR",
                        message="Feishu/Lark websocket failed; retrying",
                        data={
                            "error_type": type(exc).__name__,
                            "retry_attempt": failures,
                            "retry_delay_s": delay,
                        },
                    )
                    await self._detach_sdk(suppress_errors=True)
                    if not self._stop_event.is_set():
                        await self.sleep(delay)
        finally:
            await self._detach_sdk()

    def _create_sdk(self) -> object:
        if self.channel_factory is not None:
            return self.channel_factory(
                app_id=self.app_id,
                app_secret=self.app_secret,
                domain=self.domain,
            )
        try:
            from lark_channel import (
                ChatQueueConfig,
                FeishuChannel,
                InboundConfig,
                MediaCapabilities,
                PolicyConfig,
                SafetyConfig,
                SecurityConfig,
                TransportConfig,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Feishu support requires the optional dependency. Install imcodex with: pip install -e '.[feishu]'"
            ) from exc
        return FeishuChannel(
            app_id=self.app_id,
            app_secret=self.app_secret,
            domain=self.domain,
            transport=TransportConfig(kind="ws", auto_reconnect=True),
            policy=PolicyConfig(
                dm_policy="open",
                group_policy="open",
                require_mention=False,
            ),
            safety=SafetyConfig(chat_queue=ChatQueueConfig(enabled=False)),
            security=SecurityConfig(
                mode="strict",
                allow_insecure_ws=False,
                allow_local_insecure_ws=False,
                max_ws_fragment_parts=64,
                max_ws_fragment_bytes=2 * 1024 * 1024,
                max_concurrent_ws_handlers=16,
                resource_overflow_policy="drop",
            ),
            inbound=InboundConfig(
                expand_merge_forward=False,
                fetch_interactive_card=False,
                media_capabilities=MediaCapabilities(
                    image=False,
                    audio=False,
                    video=False,
                    file=False,
                    sticker=False,
                ),
                include_raw=False,
            ),
            name_lookup=lambda _ids: {},
        )

    def _subscribe_sdk(self, sdk: object) -> None:
        subscriptions: list[Callable[[], object]] = []
        try:
            subscriptions.append(sdk.on("message", self._queue_inbound))
            subscriptions.append(
                sdk.on(
                    "reconnecting",
                    lambda *_args: self._queue_health(False, "reconnecting"),
                )
            )
            subscriptions.append(sdk.on("reconnected", lambda *_args: self._queue_health(True, "connected")))
            subscriptions.append(sdk.on("error", lambda error: self._queue_sdk_error(error)))
        except Exception:
            for unsubscribe in subscriptions:
                unsubscribe()
            raise
        self._unsubscribe = subscriptions

    def _clear_subscriptions(self) -> None:
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:
                logger.debug("Feishu event unsubscribe failed", exc_info=True)
        self._unsubscribe = []

    def _queue_inbound(self, message: object) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed() or self._stop_event.is_set():
            return
        inbound = self.parse_inbound_message(message)
        if inbound is None:
            return
        if not self.inbound_allowed(inbound):
            suppressed = self.prepare_access_denial_report()
            if suppressed is not None:
                loop.call_soon_threadsafe(self.emit_access_denial, inbound, suppressed)
            return
        if not self._inbound_slots.acquire(blocking=False):
            with self._overflow_lock:
                self._overflow_count += 1
            return
        loop.call_soon_threadsafe(self._enqueue_inbound, inbound)

    def _enqueue_inbound(self, inbound: InboundMessage) -> None:
        queue = self._inbound_queue
        if self._stop_event.is_set() or queue is None:
            self._inbound_slots.release()
            return
        try:
            queue.put_nowait(inbound)
        except asyncio.QueueFull:
            self._inbound_slots.release()
            with self._overflow_lock:
                self._overflow_count += 1

    async def _run_inbound_worker(self) -> None:
        queue = self._inbound_queue
        if queue is None:
            return
        while True:
            inbound = await queue.get()
            try:
                self._report_inbound_overflow()
                await self.dispatch_inbound(
                    inbound,
                    reply_to_message_id=inbound.message_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feishu inbound handling failed")
            finally:
                queue.task_done()
                self._inbound_slots.release()

    def _report_inbound_overflow(self) -> None:
        loop = self._main_loop
        now = loop.time() if loop is not None else 0.0
        if now - self._last_overflow_report_at < 60.0:
            return
        with self._overflow_lock:
            dropped = self._overflow_count
            self._overflow_count = 0
        if not dropped:
            return
        self._last_overflow_report_at = now
        logger.warning("Feishu inbound queue full; dropped %d messages", dropped)
        emit_event(
            component="channels.feishu",
            event="message.inbound.queue_overflow",
            level="WARNING",
            message="Feishu inbound queue dropped messages",
            data={"dropped": dropped, "queue_limit": INBOUND_QUEUE_LIMIT},
        )

    def _drain_inbound_queue(self) -> None:
        queue = self._inbound_queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            self._inbound_slots.release()
        self._inbound_queue = None

    def _queue_health(self, connected: bool, status: str) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            partial(
                mark_channel_health,
                "feishu",
                connected=connected,
                status=status,
            )
        )

    def _queue_sdk_error(self, error: object) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._record_sdk_error,
            type(error).__name__,
        )

    def _record_sdk_error(self, error_type: str) -> None:
        snapshot = self._connection_snapshot(self._sdk) if self._sdk is not None else {}
        connected = bool(snapshot.get("ready"))
        mark_channel_health(
            "feishu",
            connected=connected,
            status="degraded" if connected else "error",
            error_type=error_type,
        )

    async def _detach_sdk(self, *, suppress_errors: bool = False) -> None:
        sdk = self._sdk
        self._clear_subscriptions()
        self._sdk = None
        if sdk is None:
            return
        try:
            await self._disconnect_sdk(sdk)
        except Exception as exc:
            if not suppress_errors:
                raise
            logger.warning("Feishu SDK disconnect failed: %s", type(exc).__name__)

    async def _disconnect_sdk(self, sdk: object) -> None:
        stop = getattr(sdk, "stop", None)
        if callable(stop):
            await asyncio.wait_for(
                asyncio.to_thread(partial(stop, join_timeout=SDK_JOIN_TIMEOUT_S)),
                timeout=SDK_DISCONNECT_TIMEOUT_S,
            )
            return
        await asyncio.wait_for(
            sdk.disconnect(),
            timeout=SDK_DISCONNECT_TIMEOUT_S,
        )

    def _strip_bot_mention(self, text: str) -> str:
        sdk = self._sdk
        identity = getattr(sdk, "bot_identity", None) if sdk is not None else None
        name = str(getattr(identity, "name", "") or "").strip()
        if not name:
            return text.strip()
        return re.sub(rf"@{re.escape(name)}\s*", "", text, count=1).strip()

    def _parse_conversation_id(self, conversation_id: str) -> tuple[str, str | None]:
        match = CONVERSATION_PATTERN.fullmatch(conversation_id)
        if match is None:
            raise ValueError(f"Unsupported Feishu conversation id: {conversation_id}")
        return match.group(1), match.group(2)

    @staticmethod
    def _connection_snapshot(sdk: object) -> dict[str, object]:
        try:
            snapshot = sdk.connection_snapshot()
        except Exception:
            return {}
        return {
            "state": getattr(snapshot, "state", None),
            "ready": getattr(snapshot, "ready", None),
        }

    @staticmethod
    def _normalize_domain(value: str) -> str:
        normalized = value.strip().lower().rstrip("/")
        if normalized in {"feishu", FEISHU_DOMAIN}:
            return FEISHU_DOMAIN
        if normalized in {"lark", "larksuite", LARK_DOMAIN}:
            return LARK_DOMAIN
        raise ValueError("Feishu domain must be 'feishu' or 'lark'.")

    @staticmethod
    def _reconnect_delay(failures: int) -> float:
        return min(
            RECONNECT_INITIAL_DELAY_S * (2 ** max(0, failures - 1)),
            RECONNECT_MAX_DELAY_S,
        )

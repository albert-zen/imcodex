from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
import hashlib
import json
import logging
from pathlib import Path
import re
from threading import BoundedSemaphore, Lock
import time
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from ..models import InboundMessage, OutboundArtifact, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .artifacts import (
    PermanentArtifactDeliveryError,
    append_artifact_failures,
    read_managed_artifact,
    record_artifact_delivery,
    stable_artifact_identity,
)
from .base import BaseChannelAdapter
from .media import (
    FileMediaMaterializer,
    FileTooLargeError,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    MAX_FILE_BYTES,
    MAX_FILE_COUNT,
    ImageMediaMaterializer,
    ImageTooLargeError,
    MediaDownloadError,
    materialize_inbound_media,
)
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
FEISHU_MEDIA_TIMEOUT = httpx.Timeout(
    20.0,
    connect=5.0,
    read=15.0,
    write=5.0,
    pool=5.0,
)
FEISHU_TOKEN_TIMEOUT = httpx.Timeout(
    10.0,
    connect=5.0,
    read=5.0,
    write=5.0,
    pool=5.0,
)
FEISHU_TOKEN_DEADLINE_S = 10.0
FEISHU_TOKEN_RESPONSE_MAX_BYTES = 64 * 1024
FEISHU_TOKEN_REFRESH_SKEW_S = 60.0
CONVERSATION_PATTERN = re.compile(r"^chat:([^:]+)(?::thread:(.+))?$")
IMAGE_PLACEHOLDER_PATTERN = re.compile(
    r"!\[([^\]\r\n]*)\]\(([^\r\n)]*)\)"
)


@dataclass(frozen=True, slots=True)
class FeishuImageReference:
    message_id: str
    file_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FeishuFileReference:
    message_id: str
    file_key: str = field(repr=False)
    filename: str = ""
    content_type: str = ""


def _config_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class FeishuChannelAdapter(BaseChannelAdapter):
    """Feishu/Lark text and image adapter over the official Channel SDK."""

    channel_id = "feishu"
    supports_outbound_artifacts = True

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
        media_dir: Path | None = None,
        outbound_media_dir: Path | None = None,
        media_materializer: ImageMediaMaterializer[FeishuImageReference] | None = None,
        file_materializer: FileMediaMaterializer[FeishuFileReference] | None = None,
        http_client: httpx.AsyncClient | None = None,
        resource_downloader: Callable[
            [FeishuImageReference, Callable[[bytes], Awaitable[None]]],
            Awaitable[None],
        ]
        | None = None,
        tenant_token_provider: Callable[[object], Awaitable[str]] | None = None,
        channel_factory: Callable[..., object] | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        super().__init__(
            middleware=middleware,
            access_policy=access_policy or ChannelAccessPolicy(),
        )
        self.enabled = enabled
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.domain = self._normalize_domain(domain)
        self.require_mention = require_mention
        self.startup_timeout_s = max(1.0, float(startup_timeout_s))
        self.channel_factory = channel_factory
        self.sleep = sleep
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._resource_downloader = resource_downloader
        self._tenant_token_provider = tenant_token_provider
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._tenant_token_lock = asyncio.Lock()
        self.outbound_media_dir = Path(
            outbound_media_dir or Path(".imcodex") / "outbound-media"
        ).resolve()
        self.media_materializer = media_materializer or ImageMediaMaterializer(
            root=media_dir or Path(".imcodex") / "channels" / "feishu" / "inbound-media",
            download=self._download_image,
        )
        self.file_materializer = file_materializer or FileMediaMaterializer(
            root=media_dir or Path(".imcodex") / "channels" / "feishu" / "inbound-media",
            download=self._download_file,
        )
        self._sdk: object | None = None
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: list[Callable[[], object]] = []
        self._inbound_queue: asyncio.Queue[
            tuple[
                InboundMessage,
                tuple[FeishuImageReference, ...],
                tuple[FeishuFileReference, ...],
            ]
        ] | None = None
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
            media_dir=Path(
                str(config.get("media_dir") or ".imcodex/channels/feishu/inbound-media")
            ),
            outbound_media_dir=Path(
                str(config.get("outbound_media_dir") or ".imcodex/outbound-media")
            ),
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        self.validate_startup_configuration()
        self._main_loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._inbound_queue = asyncio.Queue(maxsize=INBOUND_QUEUE_LIMIT)
        self._inbound_slots = BoundedSemaphore(INBOUND_QUEUE_LIMIT)
        self._sdk = self._create_sdk()
        try:
            await self.media_materializer.start()
            await self.file_materializer.start()
            self._subscribe_sdk(self._sdk)
            self._inbound_worker_task = asyncio.create_task(self._run_inbound_worker())
            if self._runner_task is None or self._runner_task.done():
                self._runner_task = asyncio.create_task(self._run_forever())
        except BaseException:
            await self.media_materializer.stop()
            await self.file_materializer.stop()
            await self._close_http_client()
            await self._detach_sdk()
            raise
        mark_channel_health(
            "feishu",
            enabled=True,
            connected=False,
            status="connecting",
            **self.access_policy_health(),
        )

    def validate_startup_configuration(self) -> None:
        if not self.enabled:
            return
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "Feishu adapter requires IMCODEX_FEISHU_APP_ID and IMCODEX_FEISHU_APP_SECRET when enabled."
            )
        if self.channel_factory is None:
            try:
                import lark_channel  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "Feishu support requires the optional dependency. Install imcodex with: pip install -e '.[feishu]'"
                ) from exc

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
            await self.media_materializer.stop()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.file_materializer.stop()
        except Exception as exc:
            errors.append(exc)
        try:
            await self._close_http_client()
        except Exception as exc:
            errors.append(exc)
        try:
            await self._detach_sdk()
        except Exception as exc:
            errors.append(exc)
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._main_loop = None
        mark_channel_health("feishu", connected=False, status="stopped")
        if errors:
            raise ExceptionGroup("Feishu shutdown failed", errors)

    def parse_inbound_message(self, message: object) -> InboundMessage | None:
        parsed = self._parse_inbound_message_with_images(message)
        return parsed[0] if parsed is not None else None

    def _parse_inbound_message_with_images(
        self,
        message: object,
    ) -> tuple[
        InboundMessage,
        tuple[FeishuImageReference, ...],
        tuple[FeishuFileReference, ...],
    ] | None:
        content_type = str(getattr(message, "raw_content_type", "") or "")
        if content_type not in {"text", "image", "post", "file"}:
            return None
        conversation = getattr(message, "conversation", None)
        sender = getattr(message, "sender", None)
        chat_id = str(getattr(conversation, "chat_id", "") or "")
        chat_type = str(getattr(conversation, "chat_type", "") or "")
        thread_id = str(getattr(conversation, "thread_id", "") or "")
        user_id = str(getattr(sender, "open_id", "") or "")
        message_id = str(getattr(message, "message_id", "") or getattr(message, "id", "") or "")
        text = str(getattr(message, "content_text", "") or "").strip()
        image_references = self._image_references(message, message_id=message_id)
        file_references = self._file_references(message, message_id=message_id)
        if content_type == "file" and not file_references:
            return None
        placeholder_matches = list(IMAGE_PLACEHOLDER_PATTERN.finditer(text))
        placeholder_keys = [
            file_key
            for match in placeholder_matches
            if self._is_downloadable_image_key(
                file_key := match.group(2).strip()
            )
        ]
        image_references = self._merge_placeholder_image_references(
            image_references,
            placeholder_keys,
            message_id=message_id,
        )
        malformed_image_placeholder = any(
            match.group(1).strip().casefold() == "image"
            and not match.group(2).strip()
            for match in placeholder_matches
        )
        if (
            (content_type == "image" and not image_references)
            or malformed_image_placeholder
        ) and len(image_references) <= MAX_IMAGE_COUNT:
            # Preserve a malformed SDK image envelope through access, dedup,
            # and topology preflight. The downloader rejects this sentinel at
            # the shared media boundary, producing a stable user-visible error
            # instead of silently dropping the platform message.
            image_references = (
                *image_references,
                FeishuImageReference(message_id=message_id, file_key=""),
            )
        if content_type in {"image", "post"}:
            text = self._strip_image_placeholders(text, image_references)
        if not chat_id or not user_id or not message_id:
            return None
        if chat_type in {"group", "topic"}:
            if self.require_mention and not bool(getattr(message, "mentioned_bot", False)):
                return None
            text = self._strip_bot_mention(text)
        if not text and not image_references and not file_references:
            return None
        conversation_id = f"chat:{chat_id}"
        if thread_id:
            conversation_id += f":thread:{thread_id}"
        return (
            InboundMessage(
                channel_id=self.channel_id,
                conversation_id=conversation_id,
                user_id=user_id,
                message_id=message_id,
                text=text,
            ),
            image_references,
            file_references,
        )

    async def handle_sdk_message(self, message: object) -> None:
        parsed = self._parse_inbound_message_with_images(message)
        if parsed is None:
            return
        inbound, image_references, file_references = parsed
        prepare_inbound = None
        if image_references or file_references:
            prepare_inbound = self._media_preparer(image_references, file_references)
        await self.dispatch_inbound(
            inbound,
            reply_to_message_id=inbound.message_id,
            prepare_inbound=prepare_inbound,
            pending_attachment_count=len(image_references) + len(file_references),
        )

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != self.channel_id:
            return
        if not message.text.strip() and not message.artifacts:
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
        artifact_failures: list[str] = []
        original_artifacts = list(message.artifacts)
        for index, artifact in enumerate(original_artifacts):
            try:
                result = await self._send_artifact(
                    sdk,
                    chat_id=chat_id,
                    artifact=artifact,
                    message=message,
                    reply_to=reply_to,
                    reply_in_thread=bool(thread_id),
                )
            except PermanentArtifactDeliveryError as exc:
                artifact_failures.append(f"{artifact.filename}: {exc}")
            except Exception:
                append_artifact_failures(message, artifact_failures)
                message.artifacts = original_artifacts[index:]
                raise
            else:
                if hasattr(result, "success") and not bool(getattr(result, "success")):
                    error = getattr(result, "error", None)
                    if self._artifact_error_is_permanent(error):
                        artifact_failures.append(
                            f"{artifact.filename}: Feishu rejected the upload"
                        )
                    else:
                        append_artifact_failures(message, artifact_failures)
                        message.artifacts = original_artifacts[index:]
                        raise RuntimeError("Feishu temporarily rejected an outbound artifact.")
                else:
                    record_artifact_delivery(
                        message,
                        artifact,
                        platform_message_id=str(
                            getattr(result, "message_id", "")
                            or getattr(result, "messageId", "")
                            or ""
                        ),
                    )
                message.artifacts = original_artifacts[index + 1 :]
        message.artifacts = []
        append_artifact_failures(message, artifact_failures)
        if not message.text.strip():
            return
        delivery_id = str(message.metadata.get("delivery_id") or "").strip()
        for index, chunk in enumerate(split_text(message.text, limit=FEISHU_TEXT_LIMIT)):
            opts: dict[str, object] = {"receive_id_type": "chat_id"}
            if reply_to:
                opts["reply_to"] = reply_to
            if thread_id:
                opts["reply_in_thread"] = True
            if delivery_id:
                opts["uuid"] = hashlib.sha256(
                    f"{delivery_id}\0text\0{index}\0{chunk}".encode("utf-8")
                ).hexdigest()[:50]
            result = await sdk.send(chat_id, {"text": chunk}, opts)
            if hasattr(result, "success") and not bool(getattr(result, "success")):
                raise RuntimeError("Feishu rejected an outbound message.")

    async def _send_artifact(
        self,
        sdk: object,
        *,
        chat_id: str,
        artifact: OutboundArtifact,
        message: OutboundMessage,
        reply_to: str,
        reply_in_thread: bool,
    ):
        _source, content = await read_managed_artifact(
            artifact,
            root=self.outbound_media_dir,
        )
        if artifact.kind == "image":
            outbound = {"image": {"source": content}}
        else:
            outbound = {
                "file": {
                    "source": content,
                    "fileName": artifact.filename,
                }
            }
        opts: dict[str, object] = {"receive_id_type": "chat_id"}
        if reply_to:
            opts["reply_to"] = reply_to
        if reply_in_thread:
            opts["reply_in_thread"] = True
        identity = stable_artifact_identity(message, artifact)
        if identity:
            opts["uuid"] = identity[:50]
        return await sdk.send(chat_id, outbound, opts)

    @staticmethod
    def _artifact_error_is_permanent(error: object) -> bool:
        if bool(getattr(error, "retryable", False)):
            return False
        code = getattr(error, "code", None)
        code_value = str(getattr(code, "value", code) or "").casefold()
        # lark-channel-sdk 1.1 folds upload transport failures and upstream
        # rejections into UPLOAD_FAILED with retryable=False. Inspect its
        # human-readable hint so only ambiguous transport failures stay
        # pending; explicit server rejection is a permanent artifact failure.
        if code_value != "upload_failed":
            return False
        hint = str(getattr(error, "hint", "") or "").casefold()
        if any(
            marker in hint
            for marker in (
                "transport error",
                "network",
                "connection",
                "timed out",
                "timeout",
                "tls",
            )
        ):
            return False
        return any(
            marker in hint
            for marker in (
                "invalid image",
                "invalid file",
                "unsupported",
                "too large",
                "file size",
                "content type",
                "file type",
                "format",
            )
        )

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
                DedupConfig,
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
            # imcodex owns stable message dedup in UnifiedChannelMiddleware.
            # The SDK has two independent dedup layers: enabled=False gates
            # its normalize Deduper, while max_entries=0 makes SeenCache
            # immediately evict every entry and therefore retain no memory.
            safety=SafetyConfig(
                dedup=DedupConfig(enabled=False, max_entries=0),
                chat_queue=ChatQueueConfig(enabled=False),
            ),
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
                    image=True,
                    audio=False,
                    video=False,
                    file=True,
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
        parsed = self._parse_inbound_message_with_images(message)
        if parsed is None:
            return
        inbound, image_references, file_references = parsed
        if not self.inbound_allowed(inbound):
            suppressed = self.prepare_access_denial_report()
            if suppressed is not None:
                loop.call_soon_threadsafe(self.emit_access_denial, inbound, suppressed)
            return
        if not self._inbound_slots.acquire(blocking=False):
            with self._overflow_lock:
                self._overflow_count += 1
            return
        loop.call_soon_threadsafe(
            self._enqueue_inbound,
            inbound,
            image_references,
            file_references,
        )

    def _enqueue_inbound(
        self,
        inbound: InboundMessage,
        image_references: tuple[FeishuImageReference, ...],
        file_references: tuple[FeishuFileReference, ...],
    ) -> None:
        queue = self._inbound_queue
        if self._stop_event.is_set() or queue is None:
            self._inbound_slots.release()
            return
        try:
            queue.put_nowait((inbound, image_references, file_references))
        except asyncio.QueueFull:
            self._inbound_slots.release()
            with self._overflow_lock:
                self._overflow_count += 1

    async def _run_inbound_worker(self) -> None:
        queue = self._inbound_queue
        if queue is None:
            return
        while True:
            inbound, image_references, file_references = await queue.get()
            try:
                self._report_inbound_overflow()
                prepare_inbound = None
                if image_references or file_references:
                    prepare_inbound = self._media_preparer(
                        image_references,
                        file_references,
                    )
                await self.dispatch_inbound(
                    inbound,
                    reply_to_message_id=inbound.message_id,
                    prepare_inbound=prepare_inbound,
                    pending_attachment_count=len(image_references) + len(file_references),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feishu inbound handling failed")
            finally:
                queue.task_done()
                self._inbound_slots.release()

    def _media_preparer(
        self,
        image_references: tuple[FeishuImageReference, ...],
        file_references: tuple[FeishuFileReference, ...],
    ) -> Callable[[InboundMessage], Awaitable[InboundMessage]]:
        async def prepare(inbound: InboundMessage) -> InboundMessage:
            return await materialize_inbound_media(
                inbound,
                image_references=image_references,
                image_materializer=self.media_materializer,
                file_references=file_references,
                file_materializer=self.file_materializer,
            )

        return prepare

    async def _download_image(
        self,
        reference: FeishuImageReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        await self._download_resource(
            reference,
            write_chunk,
            resource_type="image",
            max_bytes=MAX_IMAGE_BYTES,
        )

    async def _download_file(
        self,
        reference: FeishuFileReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        await self._download_resource(
            reference,
            write_chunk,
            resource_type="file",
            max_bytes=MAX_FILE_BYTES,
        )

    async def _download_resource(
        self,
        reference,
        write_chunk: Callable[[bytes], Awaitable[None]],
        *,
        resource_type: str,
        max_bytes: int,
    ) -> None:
        if not reference.message_id.strip() or not reference.file_key.strip():
            raise MediaDownloadError
        if self._resource_downloader is not None:
            await self._resource_downloader(reference, write_chunk)
            return
        try:
            token = await self._get_tenant_access_token()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Token acquisition failures may retain credentials in their
            # exception context. Convert to the stable media boundary without
            # preserving that chain.
            raise MediaDownloadError from None
        if not isinstance(token, str) or not token.strip():
            raise MediaDownloadError
        token = token.strip()

        message_id = quote(reference.message_id, safe="")
        file_key = quote(reference.file_key, safe="")
        url = f"{self.domain}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        client = self._ensure_http_client()
        try:
            async with client.stream(
                "GET",
                url,
                params={"type": resource_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
                timeout=FEISHU_MEDIA_TIMEOUT,
            ) as response:
                if 300 <= response.status_code < 400 or not response.is_success:
                    raise MediaDownloadError
                content_encoding = (
                    response.headers.get("Content-Encoding", "").strip().casefold()
                )
                if content_encoding not in {"", "identity"}:
                    raise MediaDownloadError
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                        if declared_size < 0:
                            raise MediaDownloadError
                        if declared_size > max_bytes:
                            if resource_type == "image":
                                raise ImageTooLargeError
                            raise FileTooLargeError
                    except ValueError:
                        raise MediaDownloadError from None
                received = False
                async for chunk in response.aiter_raw():
                    received = received or bool(chunk)
                    await write_chunk(chunk)
                if not received:
                    raise MediaDownloadError
        except MediaDownloadError:
            raise
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            # HTTP failures retain the bearer header and opaque resource key
            # on their request object. Never carry that object across the
            # stable media error boundary.
            raise MediaDownloadError from None

    def _ensure_http_client(self) -> httpx.AsyncClient:
        client = self._http_client
        if client is None or (self._owns_http_client and client.is_closed):
            client = httpx.AsyncClient()
            self._http_client = client
            self._owns_http_client = True
        return client

    async def _close_http_client(self) -> None:
        client = self._http_client
        if client is None or not self._owns_http_client:
            return
        self._http_client = None
        await client.aclose()

    async def _get_tenant_access_token(self) -> str:
        provider = self._tenant_token_provider
        if provider is not None:
            sdk = self._sdk
            if sdk is None:
                raise MediaDownloadError
            try:
                async with asyncio.timeout(FEISHU_TOKEN_DEADLINE_S):
                    token = await provider(sdk)
            except TimeoutError:
                raise MediaDownloadError from None
            if not isinstance(token, str) or not token.strip():
                raise MediaDownloadError
            return token.strip()

        async with self._tenant_token_lock:
            now = time.monotonic()
            if self._tenant_access_token and now < self._tenant_access_token_expires_at:
                return self._tenant_access_token

            client = self._ensure_http_client()
            body = bytearray()
            try:
                async with asyncio.timeout(FEISHU_TOKEN_DEADLINE_S):
                    async with client.stream(
                        "POST",
                        f"{self.domain}/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self.app_id, "app_secret": self.app_secret},
                        headers={"Accept-Encoding": "identity"},
                        follow_redirects=False,
                        timeout=FEISHU_TOKEN_TIMEOUT,
                    ) as response:
                        if 300 <= response.status_code < 400 or not response.is_success:
                            raise MediaDownloadError
                        content_encoding = (
                            response.headers.get("Content-Encoding", "")
                            .strip()
                            .casefold()
                        )
                        if content_encoding not in {"", "identity"}:
                            raise MediaDownloadError
                        if response.is_stream_consumed:
                            body.extend(response.content)
                        else:
                            async for chunk in response.aiter_raw():
                                body.extend(chunk)
                                if len(body) > FEISHU_TOKEN_RESPONSE_MAX_BYTES:
                                    raise MediaDownloadError
                        if len(body) > FEISHU_TOKEN_RESPONSE_MAX_BYTES:
                            raise MediaDownloadError
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.HTTPError, MediaDownloadError):
                # HTTP request objects contain the app secret in their JSON
                # body. Collapse every failure before it crosses the media
                # boundary or reaches channel logs.
                raise MediaDownloadError from None

        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError
            code = int(payload.get("code", -1))
            token = str(payload.get("tenant_access_token") or "").strip()
            expires_in = float(payload.get("expire") or 0)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            raise MediaDownloadError from None
        if code != 0 or not token or expires_in <= 0:
            raise MediaDownloadError
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = time.monotonic() + max(
            1.0,
            expires_in - FEISHU_TOKEN_REFRESH_SKEW_S,
        )
        return token

    @staticmethod
    def _image_references(
        message: object,
        *,
        message_id: str,
    ) -> tuple[FeishuImageReference, ...]:
        resources = getattr(message, "resources", None)
        if not isinstance(resources, (list, tuple)):
            return ()
        references: list[FeishuImageReference] = []
        seen: set[str] = set()
        for resource in resources:
            if isinstance(resource, dict):
                resource_type = str(resource.get("type") or "")
                file_key = str(resource.get("file_key") or "").strip()
            else:
                resource_type = str(getattr(resource, "type", "") or "")
                file_key = str(getattr(resource, "file_key", "") or "").strip()
            if (
                resource_type != "image"
                or not FeishuChannelAdapter._is_downloadable_image_key(file_key)
                or file_key in seen
            ):
                continue
            seen.add(file_key)
            references.append(
                FeishuImageReference(message_id=message_id, file_key=file_key)
            )
            # Preserve one item beyond the limit so the shared materializer
            # rejects the complete message without retaining an unbounded list.
            if len(references) > MAX_IMAGE_COUNT:
                break
        return tuple(references)

    @staticmethod
    def _file_references(
        message: object,
        *,
        message_id: str,
    ) -> tuple[FeishuFileReference, ...]:
        resources = getattr(message, "resources", None)
        if not isinstance(resources, (list, tuple)):
            return ()
        references: list[FeishuFileReference] = []
        seen: set[str] = set()
        for resource in resources:
            if isinstance(resource, dict):
                resource_type = str(resource.get("type") or "")
                file_key = str(resource.get("file_key") or "").strip()
                filename = str(
                    resource.get("file_name")
                    or resource.get("filename")
                    or resource.get("name")
                    or ""
                ).strip()
                content_type = str(resource.get("content_type") or "").strip()
            else:
                resource_type = str(getattr(resource, "type", "") or "")
                file_key = str(getattr(resource, "file_key", "") or "").strip()
                filename = str(
                    getattr(resource, "file_name", "")
                    or getattr(resource, "filename", "")
                    or getattr(resource, "name", "")
                    or ""
                ).strip()
                content_type = str(getattr(resource, "content_type", "") or "").strip()
            if resource_type != "file" or not file_key or file_key in seen:
                continue
            seen.add(file_key)
            references.append(
                FeishuFileReference(
                    message_id=message_id,
                    file_key=file_key,
                    filename=filename or "file",
                    content_type=content_type,
                )
            )
            if len(references) > MAX_FILE_COUNT:
                break
        return tuple(references)

    @staticmethod
    def _is_downloadable_image_key(value: str) -> bool:
        """Distinguish opaque Feishu keys from Markdown image URLs."""

        if (
            not value
            or len(value) > 1024
            or value.startswith(("/", "\\"))
            or "/" in value
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            return False
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return not (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        )

    @staticmethod
    def _merge_placeholder_image_references(
        references: tuple[FeishuImageReference, ...],
        placeholder_keys: list[str],
        *,
        message_id: str,
    ) -> tuple[FeishuImageReference, ...]:
        """Recover image keys rendered by SDK post normalization.

        lark-channel-sdk 1.1.0 renders ``content_v2`` image nodes into
        ``![image](key)`` text but omits them from ``message.resources``.
        The platform resource endpoint accepts that same key, so merge the
        normalized placeholders without depending on SDK-internal raw ASTs.
        """

        by_file_key = {reference.file_key: reference for reference in references}
        merged: list[FeishuImageReference] = []
        seen: set[str] = set()
        for file_key in placeholder_keys:
            if not file_key or file_key in seen:
                continue
            seen.add(file_key)
            merged.append(
                by_file_key.get(file_key)
                or FeishuImageReference(message_id=message_id, file_key=file_key)
            )
            # Preserve one item past the shared limit so the complete message
            # is rejected without retaining an unbounded placeholder list.
            if len(merged) > MAX_IMAGE_COUNT:
                return tuple(merged)
        # The SDK may list ordinary post image nodes before image references
        # extracted from earlier Markdown nodes. Placeholder order reflects
        # the actual rendered post, so append only resource-only images here.
        for reference in references:
            if reference.file_key in seen:
                continue
            seen.add(reference.file_key)
            merged.append(reference)
            if len(merged) > MAX_IMAGE_COUNT:
                break
        return tuple(merged)

    @staticmethod
    def _strip_image_placeholders(
        text: str,
        references: tuple[FeishuImageReference, ...],
    ) -> str:
        file_keys = {reference.file_key for reference in references}

        def replace(match: re.Match[str]) -> str:
            return "" if match.group(2).strip() in file_keys else match.group(0)

        cleaned = IMAGE_PLACEHOLDER_PATTERN.sub(replace, text)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

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

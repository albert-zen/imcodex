from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
from typing import Any, Awaitable, Callable

from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .artifacts import (
    PermanentArtifactDeliveryError,
    append_artifact_failures,
    read_managed_artifact,
    stable_artifact_identity,
)
from .base import BaseChannelAdapter
from .media import (
    MAX_IMAGE_COUNT,
    ImageMediaMaterializer,
    MediaDownloadError,
    materialize_inbound_images,
)
from .text import split_text
from .weixin_ilink import ILinkError, WeixinILinkTransport, WeixinImageReference
from .weixin_state import (
    WeixinCredentials,
    WeixinStateStore,
    WeixinTransportState,
    is_weixin_user_id,
)


logger = logging.getLogger(__name__)

WEIXIN_TEXT_LIMIT = 4000
STALE_TOKEN_CODE = -14
STALE_TOKEN_PAUSE_S = 60 * 60
RECONNECT_INITIAL_DELAY_S = 2.0
RECONNECT_MAX_DELAY_S = 30.0
CONVERSATION_PATTERN = re.compile(r"^user:([^@\s*]+@im\.wechat)$")


class WeixinChannelAdapter(BaseChannelAdapter):
    """Experimental direct-message adapter for Tencent iLink."""

    channel_id = "weixin"
    supports_outbound_artifacts = True

    def __init__(
        self,
        *,
        enabled: bool,
        middleware,
        state_dir: Path,
        access_policy: ChannelAccessPolicy | None = None,
        poll_timeout_ms: int = 35_000,
        state_store: WeixinStateStore | None = None,
        transport_factory: Callable[[WeixinCredentials], object] | None = None,
        media_dir: Path | None = None,
        outbound_media_dir: Path | None = None,
        media_materializer: ImageMediaMaterializer[WeixinImageReference] | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        super().__init__(
            middleware=middleware,
            access_policy=access_policy or ChannelAccessPolicy(),
        )
        self.enabled = enabled
        self.state_dir = Path(state_dir)
        self.poll_timeout_ms = max(5_000, min(int(poll_timeout_ms), 120_000))
        self.state_store = state_store or WeixinStateStore(self.state_dir)
        self.transport_factory = transport_factory
        self.sleep = sleep
        self._credentials: WeixinCredentials | None = None
        self._state = WeixinTransportState()
        self._transport: object | None = None
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._auth_stale = False
        self.outbound_media_dir = Path(
            outbound_media_dir or Path(".imcodex") / "outbound-media"
        ).resolve()
        self.media_materializer = media_materializer or ImageMediaMaterializer(
            root=media_dir or self.state_dir / "inbound-media",
            download=self._download_inbound_image,
        )

    @classmethod
    def from_config(cls, *, config: dict[str, object], middleware):
        state_dir = str(config.get("state_dir") or "").strip()
        if not state_dir:
            raise RuntimeError("Weixin adapter requires a state_dir.")
        return cls(
            enabled=bool(config.get("enabled")),
            middleware=middleware,
            state_dir=Path(state_dir),
            media_dir=Path(
                str(config.get("media_dir") or Path(state_dir) / "inbound-media")
            ),
            outbound_media_dir=Path(
                str(config.get("outbound_media_dir") or ".imcodex/outbound-media")
            ),
            access_policy=ChannelAccessPolicy.from_config(config),
            poll_timeout_ms=int(config.get("poll_timeout_ms") or 35_000),
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        credentials = self._validated_credentials()
        self._credentials = credentials
        if not self.access_policy.allowed_user_ids and not self.access_policy.denies_all and credentials.owner_user_id:
            self.access_policy = ChannelAccessPolicy(
                allowed_user_ids=frozenset({credentials.owner_user_id}),
                allowed_conversation_ids=self.access_policy.allowed_conversation_ids,
                access_match=self.access_policy.access_match,
            )
        self._state = self.state_store.load_transport_state()
        if self._state.account_id != credentials.account_id:
            self._state = WeixinTransportState(account_id=credentials.account_id)
            await self._persist_state()
        else:
            admitted_tokens = {
                user_id: token
                for user_id, token in self._state.context_tokens.items()
                if self.access_policy.allows(
                    user_id=user_id,
                    conversation_id=f"user:{user_id}",
                )
            }
            if admitted_tokens != self._state.context_tokens:
                self._state.context_tokens = admitted_tokens
                await self._persist_state()
        transport = self._create_transport(credentials)
        self._transport = transport
        try:
            await self.media_materializer.start()
        except BaseException:
            self._transport = None
            try:
                await transport.close()
            except Exception as exc:
                logger.warning(
                    "Weixin transport cleanup after media startup failure failed: %s",
                    type(exc).__name__,
                )
            raise
        self._stop_event.clear()
        self._auth_stale = False
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_forever())
        mark_channel_health(
            "weixin",
            enabled=True,
            connected=False,
            status="connecting",
            experimental=True,
            **self.access_policy_health(),
        )

    def validate_startup_configuration(self) -> None:
        if not self.enabled:
            return
        self._validated_credentials()
        self.state_store.load_transport_state()

    def _validated_credentials(self) -> WeixinCredentials:
        credentials = self.state_store.load_credentials()
        if credentials is None:
            raise RuntimeError("Weixin is not logged in. Run: python -m imcodex channels login weixin")
        return credentials

    async def stop(self) -> None:
        errors: list[Exception] = []
        self._stop_event.set()
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
        transport = self._transport
        self._transport = None
        if transport is not None:
            try:
                await asyncio.wait_for(transport.notify_stop(), timeout=5.0)
            except Exception as exc:
                logger.warning("Weixin notifyStop failed: %s", type(exc).__name__)
            try:
                await transport.close()
            except Exception as exc:
                errors.append(exc)
        mark_channel_health("weixin", connected=False, status="stopped", experimental=True)
        if errors:
            raise ExceptionGroup("Weixin shutdown failed", errors)

    def parse_inbound_message(self, payload: dict[str, Any]) -> InboundMessage | None:
        parsed = self._parse_inbound_message(payload)
        return parsed[0] if parsed is not None else None

    def _parse_inbound_message(
        self,
        payload: dict[str, Any],
    ) -> tuple[InboundMessage, tuple[WeixinImageReference, ...]] | None:
        if self._int_value(payload.get("message_type")) != 1:
            return None
        if str(payload.get("group_id") or "").strip():
            return None
        user_id = str(payload.get("from_user_id") or "").strip()
        if not is_weixin_user_id(user_id):
            return None
        message_id = self._stable_message_id(payload)
        text = self._text_from_items(payload.get("item_list"))
        image_references = self._image_references_from_items(payload.get("item_list"))
        if not message_id or (not text and not image_references):
            return None
        return (
            InboundMessage(
                channel_id=self.channel_id,
                conversation_id=f"user:{user_id}",
                user_id=user_id,
                message_id=message_id,
                text=text,
            ),
            image_references,
        )

    async def handle_raw_message(self, payload: dict[str, Any]) -> None:
        if self._int_value(payload.get("message_type")) != 1:
            return
        if str(payload.get("group_id") or "").strip():
            return
        user_id = str(payload.get("from_user_id") or "").strip()
        context_token = str(payload.get("context_token") or "").strip()
        conversation_id = f"user:{user_id}"
        if (
            is_weixin_user_id(user_id)
            and context_token
            and self.access_policy.allows(user_id=user_id, conversation_id=conversation_id)
        ):
            self._state.set_context_token(user_id, context_token)
            await self._persist_state()
        parsed = self._parse_inbound_message(payload)
        if parsed is None:
            return
        inbound, image_references = parsed
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
        if not self.enabled or message.channel_id != self.channel_id:
            return
        if not message.text.strip() and not message.artifacts:
            return
        self.ensure_outbound_allowed(message)
        if self._auth_stale:
            raise RuntimeError("Weixin credentials are stale. Run: python -m imcodex channels login weixin")
        transport = self._transport
        if transport is None:
            raise RuntimeError("Weixin channel is not connected.")
        user_id = self._parse_conversation_id(message.conversation_id)
        context_token = self._state.context_tokens.get(user_id)
        if not context_token:
            raise RuntimeError("Weixin cannot send before this user has supplied an active context token.")
        delivery_id = str(message.metadata.get("delivery_id") or "").strip()
        artifact_failures: list[str] = []
        original_artifacts = list(message.artifacts)
        for index, artifact in enumerate(original_artifacts):
            try:
                _source, content = await read_managed_artifact(
                    artifact,
                    root=self.outbound_media_dir,
                )
                identity = stable_artifact_identity(message, artifact)
                await transport.send_artifact(
                    to_user_id=user_id,
                    content=content,
                    filename=artifact.filename,
                    kind=artifact.kind,
                    context_token=context_token,
                    client_id=(f"imcodex-artifact-{identity[:32]}" if identity else None),
                )
            except PermanentArtifactDeliveryError as exc:
                artifact_failures.append(f"{artifact.filename}: {exc}")
            except ILinkError as exc:
                if exc.code == STALE_TOKEN_CODE:
                    self._mark_stale_token()
                    append_artifact_failures(message, artifact_failures)
                    message.artifacts = original_artifacts[index:]
                    raise
                if exc.code in {413, 415, 422}:
                    artifact_failures.append(
                        f"{artifact.filename}: Weixin rejected the upload"
                    )
                else:
                    append_artifact_failures(message, artifact_failures)
                    message.artifacts = original_artifacts[index:]
                    raise
            except Exception:
                append_artifact_failures(message, artifact_failures)
                message.artifacts = original_artifacts[index:]
                raise
            else:
                message.artifacts = original_artifacts[index + 1 :]
        message.artifacts = []
        append_artifact_failures(message, artifact_failures)
        if not message.text.strip():
            return
        for index, chunk in enumerate(split_text(message.text, limit=WEIXIN_TEXT_LIMIT)):
            try:
                await transport.send_text(
                    to_user_id=user_id,
                    text=chunk,
                    context_token=context_token,
                    client_id=f"{delivery_id}:{index}" if delivery_id else None,
                )
            except ILinkError as exc:
                if exc.code == STALE_TOKEN_CODE:
                    self._mark_stale_token()
                raise

    async def _run_forever(self) -> None:
        transport = self._transport
        if transport is None:
            return
        try:
            try:
                await transport.notify_start()
            except Exception as exc:
                logger.warning(
                    "Weixin notifyStart failed; polling will continue: %s",
                    type(exc).__name__,
                )
            failures = 0
            while not self._stop_event.is_set():
                try:
                    await self._poll_once()
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except ILinkError as exc:
                    if exc.code == STALE_TOKEN_CODE:
                        self._mark_stale_token()
                        failures = 0
                        await self.sleep(STALE_TOKEN_PAUSE_S)
                        continue
                    failures += 1
                    await self._handle_poll_failure(exc, failures)
                except Exception as exc:
                    failures += 1
                    await self._handle_poll_failure(exc, failures)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        transport = self._transport
        if transport is None:
            raise RuntimeError("Weixin transport is unavailable.")
        response = await transport.get_updates(
            get_updates_buf=self._state.get_updates_buf,
            timeout_ms=self.poll_timeout_ms,
        )
        self._raise_response_error(response)
        self._auth_stale = False
        suggested_timeout = response.get("longpolling_timeout_ms")
        if isinstance(suggested_timeout, (int, float)) and suggested_timeout > 0:
            self.poll_timeout_ms = max(5_000, min(int(suggested_timeout), 120_000))
        mark_channel_health(
            "weixin",
            connected=True,
            status="connected",
            experimental=True,
        )
        messages = response.get("msgs")
        if isinstance(messages, list):
            for payload in messages:
                if isinstance(payload, dict):
                    await self.handle_raw_message(payload)
        next_buf = response.get("get_updates_buf")
        if isinstance(next_buf, str) and next_buf and next_buf != self._state.get_updates_buf:
            self._state.get_updates_buf = next_buf
            await self._persist_state()

    async def _handle_poll_failure(self, exc: Exception, failures: int) -> None:
        delay = self._reconnect_delay(failures)
        logger.warning("Weixin polling failed; retrying in %.1fs: %s", delay, type(exc).__name__)
        mark_channel_health(
            "weixin",
            connected=False,
            status="reconnecting",
            error_type=type(exc).__name__,
            retry_delay_s=delay,
            experimental=True,
        )
        emit_event(
            component="channels.weixin",
            event="weixin.polling.failed",
            level="ERROR",
            message="Weixin iLink polling failed; retrying",
            data={
                "error_type": type(exc).__name__,
                "retry_attempt": failures,
                "retry_delay_s": delay,
            },
        )
        if not self._stop_event.is_set():
            await self.sleep(delay)

    def _mark_stale_token(self) -> None:
        self._auth_stale = True
        mark_channel_health(
            "weixin",
            connected=False,
            status="auth_required",
            error_code=STALE_TOKEN_CODE,
            experimental=True,
        )
        emit_event(
            component="channels.weixin",
            event="weixin.credentials.stale",
            level="ERROR",
            message="Weixin iLink credentials are stale; QR login is required",
            data={"error_code": STALE_TOKEN_CODE},
        )

    async def _persist_state(self) -> None:
        await asyncio.to_thread(self.state_store.save_transport_state, self._state)

    async def _download_inbound_image(
        self,
        reference: WeixinImageReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        transport = self._transport
        download = getattr(transport, "download_image", None)
        if not callable(download):
            raise MediaDownloadError
        await download(reference, write_chunk)

    async def _materialize_inbound(
        self,
        inbound: InboundMessage,
        image_references: tuple[WeixinImageReference, ...],
    ) -> InboundMessage:
        return await materialize_inbound_images(
            inbound,
            image_references,
            self.media_materializer,
        )

    def _create_transport(self, credentials: WeixinCredentials) -> object:
        if self.transport_factory is not None:
            return self.transport_factory(credentials)
        return WeixinILinkTransport.from_credentials(credentials)

    @staticmethod
    def _raise_response_error(response: dict[str, Any]) -> None:
        code_value = response.get("errcode")
        if code_value in (None, 0):
            code_value = response.get("ret")
        try:
            code = int(code_value or 0)
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise ILinkError(f"iLink getupdates failed with code {code}", code=code)

    @staticmethod
    def _stable_message_id(payload: dict[str, Any]) -> str:
        for field in ("message_id", "client_id", "seq"):
            value = payload.get(field)
            if value not in (None, "", 0):
                return str(value)
        return ""

    @staticmethod
    def _text_from_items(value: object) -> str:
        if not isinstance(value, list):
            return ""
        for item in value:
            if not isinstance(item, dict) or WeixinChannelAdapter._int_value(item.get("type")) != 1:
                continue
            text_item = item.get("text_item")
            if isinstance(text_item, dict):
                text = str(text_item.get("text") or "").strip()
                if text:
                    return text
        return ""

    @classmethod
    def _image_references_from_items(
        cls,
        value: object,
    ) -> tuple[WeixinImageReference, ...]:
        if not isinstance(value, list):
            return ()
        references: list[WeixinImageReference] = []
        for item in value:
            if not isinstance(item, dict) or cls._int_value(item.get("type")) != 2:
                continue
            image_item = item.get("image_item")
            image_item = image_item if isinstance(image_item, dict) else {}
            media = image_item.get("media")
            media = media if isinstance(media, dict) else {}
            references.append(
                WeixinImageReference(
                    encrypted_query_param=cls._string_value(
                        media.get("encrypt_query_param")
                    ),
                    aes_key=cls._string_value(media.get("aes_key")),
                    full_url=cls._string_value(media.get("full_url")),
                    aeskey=cls._string_value(image_item.get("aeskey")),
                )
            )
            # Preserve one item beyond the common limit so the whole message
            # is rejected rather than silently dropping later images.
            if len(references) > MAX_IMAGE_COUNT:
                break
        return tuple(references)

    @staticmethod
    def _string_value(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _int_value(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_conversation_id(conversation_id: str) -> str:
        match = CONVERSATION_PATTERN.fullmatch(conversation_id)
        if match is None:
            raise ValueError(f"Unsupported Weixin conversation id: {conversation_id}")
        return match.group(1)

    def _conversation_user_id(self, conversation_id: str) -> str | None:
        match = CONVERSATION_PATTERN.fullmatch(conversation_id)
        return match.group(1) if match is not None else None

    @staticmethod
    def _reconnect_delay(failures: int) -> float:
        return min(
            RECONNECT_INITIAL_DELAY_S * (2 ** max(0, failures - 1)),
            RECONNECT_MAX_DELAY_S,
        )

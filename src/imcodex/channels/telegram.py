from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from ..config import validate_http_endpoint
from ..models import InboundMessage, OutboundArtifact, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .artifacts import (
    PermanentArtifactDeliveryError,
    append_artifact_failures,
    read_managed_artifact,
    record_artifact_delivery,
)
from .base import BaseChannelAdapter
from .media import (
    FileMediaMaterializer,
    ImageMediaMaterializer,
    MAX_IMAGE_COUNT,
    MediaDownloadError,
    MAX_FILE_COUNT,
    materialize_inbound_media,
)
from .text import split_text


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
TELEGRAM_TEXT_LIMIT = 4000
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
OFFSET_MAX_AGE_S = 6 * 24 * 60 * 60
CONVERSATION_PATTERN = re.compile(r"^chat:(-?\d+)(?::topic:(\d+))?$")
TELEGRAM_MEDIA_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)
IMAGE_DOCUMENT_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


@dataclass(frozen=True, slots=True)
class TelegramImageReference:
    file_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TelegramFileReference:
    file_id: str = field(repr=False)
    filename: str = ""
    content_type: str = ""


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        *,
        error_code: int,
        description: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Telegram API error {error_code}: {description}")
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


def _config_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _encode_telegram_file_path(value: object) -> str:
    """Return a canonical relative Telegram file path or fail closed.

    The bot token is embedded in the download URL. Treating `file_path` as an
    arbitrary URL, accepting a redirect, or allowing an absolute/local Bot API
    path could disclose that credential or bypass the managed media spool.
    """

    raw = str(value or "").strip()
    if not raw or len(raw) > 4096 or raw.startswith(("/", "\\")) or "\\" in raw:
        raise MediaDownloadError
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise MediaDownloadError

    encoded_segments: list[str] = []
    for segment in raw.split("/"):
        if not segment:
            raise MediaDownloadError
        decoded = segment
        for _ in range(16):
            next_value = unquote(decoded)
            if next_value == decoded:
                break
            decoded = next_value
        else:
            raise MediaDownloadError
        if (
            decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        ):
            raise MediaDownloadError
        encoded_segments.append(quote(decoded, safe="-._~"))
    return "/".join(encoded_segments)


def read_telegram_bot_token_file(path: Path) -> str:
    try:
        if os.name != "nt":
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError(f"Telegram bot token file must be a non-symlink private file (0600): {path}")
        token = path.read_text(encoding="utf-8").strip()
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Could not read Telegram bot token file: {path}") from exc
    if not token:
        raise RuntimeError(f"Telegram bot token file is empty: {path}")
    return token


class TelegramChannelAdapter(BaseChannelAdapter):
    channel_id = "telegram"
    supports_outbound_artifacts = True

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str,
        middleware,
        bot_token_file: Path | None = None,
        api_base: str = DEFAULT_API_BASE,
        access_policy: ChannelAccessPolicy | None = None,
        require_mention: bool = True,
        poll_timeout_s: int = 30,
        state_dir: Path | None = None,
        media_dir: Path | None = None,
        outbound_media_dir: Path | None = None,
        media_materializer: ImageMediaMaterializer[TelegramImageReference] | None = None,
        file_materializer: FileMediaMaterializer[TelegramFileReference] | None = None,
        media_cleanup_sleep=asyncio.sleep,
        http_client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
        clock=time.time,
    ) -> None:
        super().__init__(
            middleware=middleware,
            access_policy=access_policy or ChannelAccessPolicy(),
        )
        self.enabled = enabled
        self.bot_token = bot_token.strip()
        self.bot_token_file = bot_token_file
        self.api_base = api_base.strip().rstrip("/")
        self.require_mention = require_mention
        self.poll_timeout_s = max(1, int(poll_timeout_s))
        self.state_dir = state_dir
        self.outbound_media_dir = Path(
            outbound_media_dir or Path(".imcodex") / "outbound-media"
        ).resolve()
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
        self.media_materializer = media_materializer or ImageMediaMaterializer(
            root=media_dir or Path(".imcodex") / "channels" / "telegram" / "inbound-media",
            download=self._download_image,
            clock=clock,
            cleanup_sleep=media_cleanup_sleep,
        )
        self.file_materializer = file_materializer or FileMediaMaterializer(
            root=media_dir or Path(".imcodex") / "channels" / "telegram" / "inbound-media",
            download=self._download_file,
            cleanup_sleep=media_cleanup_sleep,
        )
        self.sleep = sleep
        self.clock = clock
        self._runner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._offset, self._offset_bot_id, self._offset_updated_at = self._load_offset()
        self._bot_id: str | None = None
        self._bot_username: str | None = None

    @classmethod
    def from_config(cls, *, config: dict[str, object], middleware):
        token_file = str(config.get("bot_token_file") or "").strip()
        state_dir = str(config.get("state_dir") or "").strip()
        media_dir = str(config.get("media_dir") or "").strip()
        outbound_media_dir = str(config.get("outbound_media_dir") or "").strip()
        return cls(
            enabled=bool(config.get("enabled")),
            bot_token=str(config.get("bot_token") or ""),
            bot_token_file=Path(token_file) if token_file else None,
            middleware=middleware,
            api_base=str(config.get("api_base") or DEFAULT_API_BASE),
            access_policy=ChannelAccessPolicy.from_config(config),
            require_mention=_config_bool(config.get("require_mention"), True),
            poll_timeout_s=int(config.get("poll_timeout_s") or 30),
            state_dir=Path(state_dir) if state_dir else None,
            media_dir=Path(media_dir) if media_dir else None,
            outbound_media_dir=(
                Path(outbound_media_dir) if outbound_media_dir else None
            ),
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        self.validate_startup_configuration()
        self.bot_token = self._resolve_bot_token()
        await self.media_materializer.start()
        await self.file_materializer.start()
        self._stop_event.clear()
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_forever())
        mark_channel_health(
            "telegram",
            enabled=True,
            connected=False,
            status="connecting",
            **self.access_policy_health(),
        )

    def validate_startup_configuration(self) -> None:
        if not self.enabled:
            return
        if not self._resolve_bot_token():
            raise RuntimeError(
                "Telegram adapter requires IMCODEX_TELEGRAM_BOT_TOKEN or IMCODEX_TELEGRAM_BOT_TOKEN_FILE when enabled."
            )
        validate_http_endpoint(self.api_base, key="IMCODEX_TELEGRAM_API_BASE")

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
        try:
            await self.file_materializer.stop()
        except Exception as exc:
            errors.append(exc)
        if self._owns_http_client:
            try:
                await self.http_client.aclose()
            except Exception as exc:
                errors.append(exc)
        mark_channel_health("telegram", connected=False, status="stopped")
        if errors:
            raise ExceptionGroup("Telegram shutdown failed", errors)

    def parse_inbound_update(self, update: dict[str, Any]) -> tuple[InboundMessage, str] | None:
        parsed = self._parse_inbound_update(update)
        if parsed is None:
            return None
        inbound, reply_to_message_id, _image_references, _file_references = parsed
        return inbound, reply_to_message_id

    def _parse_inbound_update(
        self,
        update: dict[str, Any],
    ) -> tuple[
        InboundMessage,
        str,
        tuple[TelegramImageReference, ...],
        tuple[TelegramFileReference, ...],
    ] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict) or sender.get("is_bot"):
            return None
        text = str(message.get("text") or message.get("caption") or "").strip()
        image_references = self._parse_image_references(message)
        file_references = self._parse_file_references(message)
        if not text and not image_references and not file_references:
            return None
        sender_id = str(sender.get("id") or "")
        chat_id = str(chat.get("id") or "")
        message_id = str(message.get("message_id") or "")
        if not sender_id or not chat_id or not message_id:
            return None

        chat_type = str(chat.get("type") or "")
        if chat_type in {"group", "supergroup"}:
            if self.require_mention and not self._is_group_message_for_bot(message, text):
                return None
            text = self._strip_bot_mention(text)
            if not text and not image_references and not file_references:
                return None

        conversation_id = f"chat:{chat_id}"
        thread_id = message.get("message_thread_id")
        if message.get("is_topic_message") and thread_id is not None:
            conversation_id += f":topic:{thread_id}"
        inbound = InboundMessage(
            channel_id=self.channel_id,
            conversation_id=conversation_id,
            user_id=sender_id,
            message_id=f"{chat_id}:{message_id}",
            text=text,
        )
        return inbound, message_id, image_references, file_references

    async def handle_update(self, update: dict[str, Any]) -> None:
        parsed = self._parse_inbound_update(update)
        if parsed is None:
            return
        inbound, reply_to_message_id, image_references, file_references = parsed
        prepare_inbound = None
        if image_references or file_references:
            prepare_inbound = lambda message: materialize_inbound_media(
                message,
                image_references=image_references,
                image_materializer=self.media_materializer,
                file_references=file_references,
                file_materializer=self.file_materializer,
            )
        await self.dispatch_inbound(
            inbound,
            reply_to_message_id=reply_to_message_id,
            prepare_inbound=prepare_inbound,
            pending_attachment_count=len(image_references) + len(file_references),
        )

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != self.channel_id:
            return
        if not message.text.strip() and not message.artifacts:
            return
        self.ensure_outbound_allowed(message)
        chat_id, thread_id = self._parse_conversation_id(message.conversation_id)
        reply_to = self._parse_reply_message_id(
            message.metadata.get("reply_to_message_id") or message.metadata.get("message_id")
        )
        artifact_failures: list[str] = []
        original_artifacts = list(message.artifacts)
        for index, artifact in enumerate(original_artifacts):
            try:
                result = await self._send_artifact(
                    artifact,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
            except PermanentArtifactDeliveryError as exc:
                artifact_failures.append(f"{artifact.filename}: {exc}")
            except TelegramAPIError as exc:
                if self._artifact_error_is_permanent(exc):
                    artifact_failures.append(
                        f"{artifact.filename}: Telegram rejected the upload"
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
                platform_id = ""
                if isinstance(result, dict):
                    platform_id = str(result.get("message_id") or "")
                record_artifact_delivery(
                    message,
                    artifact,
                    platform_message_id=platform_id,
                )
                message.artifacts = original_artifacts[index + 1 :]
        message.artifacts = []
        append_artifact_failures(message, artifact_failures)
        if not message.text.strip():
            return
        for index, chunk in enumerate(split_text(message.text, limit=TELEGRAM_TEXT_LIMIT)):
            body: dict[str, object] = {
                "chat_id": chat_id,
                "text": chunk,
                "link_preview_options": {"is_disabled": True},
            }
            if thread_id is not None:
                body["message_thread_id"] = thread_id
            if index == 0 and reply_to is not None:
                body["reply_parameters"] = {"message_id": reply_to}
            await self._api_call(
                "sendMessage",
                body,
                max_attempts=3,
                retry_ambiguous=False,
            )

    async def _send_artifact(
        self,
        artifact: OutboundArtifact,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to: int | None,
    ) -> object:
        _source, content = await read_managed_artifact(
            artifact,
            root=self.outbound_media_dir,
        )
        field = "photo" if artifact.kind == "image" else "document"
        method = "sendPhoto" if artifact.kind == "image" else "sendDocument"
        body: dict[str, object] = {"chat_id": chat_id}
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": reply_to}
        return await self._api_upload(
            method,
            body,
            field=field,
            filename=artifact.filename,
            content=content,
            content_type=artifact.content_type,
        )

    async def _run_forever(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                await self._probe_bot()
                mark_channel_health(
                    "telegram",
                    connected=True,
                    status="connected",
                    bot_username=self._bot_username,
                )
                emit_event(
                    component="channels.telegram",
                    event="telegram.polling.ready",
                    message="Telegram long polling is ready",
                    data={"bot_username": self._bot_username},
                )
                while not self._stop_event.is_set():
                    await self._poll_once()
                    failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = self._reconnect_delay(failures)
                if isinstance(exc, TelegramAPIError) and exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                logger.warning("Telegram polling failed; retrying in %.1fs: %s", delay, exc)
                logger.debug("Telegram polling failure details", exc_info=True)
                mark_channel_health(
                    "telegram",
                    connected=False,
                    status="reconnecting",
                    error_type=type(exc).__name__,
                    retry_delay_s=delay,
                )
                emit_event(
                    component="channels.telegram",
                    event="telegram.polling.failed",
                    level="ERROR",
                    message="Telegram polling failed; retrying",
                    data={
                        "error_type": type(exc).__name__,
                        "retry_attempt": failures,
                        "retry_delay_s": delay,
                    },
                )
                if not self._stop_event.is_set():
                    await self.sleep(delay)

    async def _probe_bot(self) -> None:
        result = await self._api_call("getMe", {}, max_attempts=1)
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Telegram getMe response did not include a bot id.")
        bot_id = str(result["id"])
        if self._offset_bot_id != bot_id or self._offset_is_stale():
            self._offset = None
            self._offset_bot_id = bot_id
            self._offset_updated_at = self.clock()
            await self._persist_offset()
        self._bot_id = bot_id
        self._bot_username = str(result.get("username") or "").strip() or None

    async def _poll_once(self) -> None:
        if self._offset is not None and self._offset_is_stale():
            self._offset = None
            self._offset_updated_at = self.clock()
            await self._persist_offset()
        body: dict[str, object] = {
            "timeout": self.poll_timeout_s,
            "allowed_updates": ["message"],
        }
        if self._offset is not None:
            body["offset"] = self._offset
        result = await self._api_call(
            "getUpdates",
            body,
            timeout_s=self.poll_timeout_s + 10,
            max_attempts=1,
        )
        if not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates response was not a list.")
        for update in result:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            await self.handle_update(update)
            self._offset = max(self._offset or 0, update_id + 1)
            self._offset_bot_id = self._bot_id or self._offset_bot_id
            self._offset_updated_at = self.clock()
            await self._persist_offset()

    async def _api_call(
        self,
        method: str,
        body: dict[str, object],
        *,
        timeout_s: float = 20.0,
        max_attempts: int,
        retry_ambiguous: bool = True,
    ) -> object:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await self.http_client.post(url, json=body, timeout=timeout_s)
            except httpx.HTTPError as exc:
                if not retry_ambiguous or attempt >= attempts:
                    raise TelegramAPIError(
                        error_code=0,
                        description=f"network request failed ({type(exc).__name__})",
                    ) from None
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue

            payload = self._response_payload(response)
            error_code = int(payload.get("error_code") or response.status_code or 0)
            retry_after = self._retry_after(payload, response)
            if error_code == 429:
                if attempt < attempts:
                    await self.sleep(retry_after)
                    continue
                description = str(payload.get("description") or "rate limited")
                raise TelegramAPIError(
                    error_code=error_code,
                    description=description,
                    retry_after=retry_after,
                )
            if response.status_code >= 500 and retry_ambiguous and attempt < attempts:
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if not response.is_success or payload.get("ok") is not True:
                description = str(payload.get("description") or "request failed")
                raise TelegramAPIError(error_code=error_code, description=description)
            return payload.get("result")
        raise RuntimeError("Telegram API request exhausted retry attempts.")

    async def _api_upload(
        self,
        method: str,
        body: dict[str, object],
        *,
        field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> object:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        form = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
            for key, value in body.items()
        }
        try:
            response = await self.http_client.post(
                url,
                data=form,
                files={field: (filename, content, content_type)},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise TelegramAPIError(
                error_code=0,
                description=f"network request failed ({type(exc).__name__})",
            ) from None
        payload = self._response_payload(response)
        error_code = int(payload.get("error_code") or response.status_code or 0)
        if not response.is_success or payload.get("ok") is not True:
            raise TelegramAPIError(
                error_code=error_code,
                description=str(payload.get("description") or "upload failed"),
                retry_after=self._retry_after(payload, response) if error_code == 429 else None,
            )
        return payload.get("result")

    @staticmethod
    def _artifact_error_is_permanent(error: TelegramAPIError) -> bool:
        if error.error_code not in {400, 413, 415, 422}:
            return False
        description = error.description.casefold()
        return any(
            marker in description
            for marker in (
                "file is too big",
                "file too large",
                "image_process_failed",
                "photo_invalid_dimensions",
                "wrong file type",
                "unsupported media",
                "invalid media",
            )
        )

    async def _download_image(
        self,
        reference: TelegramImageReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        if not reference.file_id.strip():
            raise MediaDownloadError
        result = await self._api_call(
            "getFile",
            {"file_id": reference.file_id},
            timeout_s=20.0,
            max_attempts=1,
        )
        if not isinstance(result, dict):
            raise MediaDownloadError
        encoded_path = _encode_telegram_file_path(result.get("file_path"))
        parsed_base = urlsplit(self.api_base)
        token = quote(self.bot_token, safe=":-_")
        base_path = parsed_base.path.rstrip("/")
        download_url = urlunsplit(
            (
                parsed_base.scheme,
                parsed_base.netloc,
                f"{base_path}/file/bot{token}/{encoded_path}",
                "",
                "",
            )
        )
        try:
            async with self.http_client.stream(
                "GET",
                download_url,
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
                timeout=TELEGRAM_MEDIA_TIMEOUT,
            ) as response:
                if 300 <= response.status_code < 400 or not response.is_success:
                    raise MediaDownloadError
                content_encoding = response.headers.get("Content-Encoding", "").strip().casefold()
                if content_encoding not in {"", "identity"}:
                    raise MediaDownloadError
                if response.is_stream_consumed:
                    # Mock/custom transports may hand httpx an already-loaded
                    # identity body. Real network responses stay on raw stream.
                    await write_chunk(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        await write_chunk(chunk)
        except MediaDownloadError:
            raise
        except httpx.HTTPError:
            # httpx exceptions retain the request URL, which contains the bot
            # token. Keep that credential out of any later exception chain.
            raise MediaDownloadError from None

    async def _download_file(
        self,
        reference: TelegramFileReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        await self._download_image(
            TelegramImageReference(file_id=reference.file_id),
            write_chunk,
        )

    @staticmethod
    def _parse_image_references(
        message: dict[str, Any],
    ) -> tuple[TelegramImageReference, ...]:
        references: list[TelegramImageReference] = []
        photo = message.get("photo")
        saw_photo_envelope = isinstance(photo, list) and bool(photo)
        if isinstance(photo, list):
            candidates: list[tuple[int, int, int, str]] = []
            for index, item in enumerate(photo):
                if not isinstance(item, dict):
                    continue
                file_id = str(item.get("file_id") or "").strip()
                if not file_id:
                    continue
                width = _nonnegative_int(item.get("width"))
                height = _nonnegative_int(item.get("height"))
                file_size = _nonnegative_int(item.get("file_size"))
                candidates.append((width * height, file_size, index, file_id))
            if candidates:
                references.append(TelegramImageReference(file_id=max(candidates)[-1]))
            elif saw_photo_envelope:
                references.append(TelegramImageReference(file_id=""))

        if not references:
            document = message.get("document")
            if isinstance(document, dict):
                file_id = str(document.get("file_id") or "").strip()
                mime_type = str(document.get("mime_type") or "").partition(";")[0].strip().casefold()
                file_name = str(document.get("file_name") or "").strip()
                suffix = Path(file_name).suffix.casefold()
                if mime_type.startswith("image/") or suffix in IMAGE_DOCUMENT_SUFFIXES:
                    references.append(TelegramImageReference(file_id=file_id))

        return tuple(references[:MAX_IMAGE_COUNT])

    @staticmethod
    def _parse_file_references(
        message: dict[str, Any],
    ) -> tuple[TelegramFileReference, ...]:
        document = message.get("document")
        if not isinstance(document, dict):
            return ()
        file_id = str(document.get("file_id") or "").strip()
        filename = str(document.get("file_name") or "").strip()
        content_type = str(document.get("mime_type") or "").partition(";")[0].strip()
        suffix = Path(filename).suffix.casefold()
        if content_type.casefold().startswith("image/") or suffix in IMAGE_DOCUMENT_SUFFIXES:
            return ()
        if not filename:
            filename = "file"
        return (
            TelegramFileReference(
                file_id=file_id,
                filename=filename,
                content_type=content_type,
            ),
        )[:MAX_FILE_COUNT]

    def _response_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _retry_after(self, payload: dict[str, Any], response: httpx.Response) -> float:
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
            try:
                return max(0.0, float(parameters["retry_after"]))
            except (TypeError, ValueError):
                pass
        try:
            return max(0.0, float(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1.0

    def _is_group_message_for_bot(self, message: dict[str, Any], text: str) -> bool:
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0]
            if "@" not in command:
                return True
            target = command.rsplit("@", 1)[-1]
            return bool(self._bot_username and target.casefold() == self._bot_username.casefold())
        if self._bot_username and re.search(
            rf"@{re.escape(self._bot_username)}\b",
            text,
            flags=re.IGNORECASE,
        ):
            return True
        reply = message.get("reply_to_message")
        if isinstance(reply, dict):
            author = reply.get("from")
            if isinstance(author, dict) and self._bot_id and str(author.get("id")) == self._bot_id:
                return True
        return False

    def _strip_bot_mention(self, text: str) -> str:
        if not self._bot_username:
            return text.strip()
        return re.sub(
            rf"@{re.escape(self._bot_username)}\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    def _parse_conversation_id(self, conversation_id: str) -> tuple[int, int | None]:
        match = CONVERSATION_PATTERN.fullmatch(conversation_id)
        if match is None:
            raise ValueError(f"Unsupported Telegram conversation id: {conversation_id}")
        chat_id = int(match.group(1))
        thread_id = int(match.group(2)) if match.group(2) is not None else None
        return chat_id, thread_id

    def _conversation_user_id(self, conversation_id: str) -> str | None:
        match = CONVERSATION_PATTERN.fullmatch(conversation_id)
        if match is None:
            return None
        chat_id = int(match.group(1))
        return str(chat_id) if chat_id > 0 and match.group(2) is None else None

    def _parse_reply_message_id(self, value: object) -> int | None:
        if value is None:
            return None
        candidate = str(value).rsplit(":", 1)[-1]
        try:
            return int(candidate)
        except ValueError:
            return None

    def _resolve_bot_token(self) -> str:
        if self.bot_token:
            return self.bot_token
        if self.bot_token_file is None:
            return ""
        return read_telegram_bot_token_file(self.bot_token_file)

    def _load_offset(self) -> tuple[int | None, str, float]:
        path = self._offset_path
        if path is None or not path.exists():
            return None, "", 0.0
        if path.is_symlink():
            raise RuntimeError(f"Telegram polling offset must not be a symlink: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("offset state must be an object")
            offset = payload.get("offset")
            bot_id = str(payload.get("bot_id") or "")
            updated_at = float(payload.get("updated_at") or 0.0)
            resolved_offset = int(offset) if offset is not None else None
            if resolved_offset is not None and (resolved_offset < 0 or not bot_id):
                raise ValueError("offset state lacks a valid bot identity")
            if updated_at < 0:
                raise ValueError("offset timestamp must be non-negative")
            return resolved_offset, bot_id, updated_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise RuntimeError(
                f"Invalid Telegram polling offset state: {path}. "
                "Inspect it before removing the file to reset polling explicitly."
            ) from None

    async def _persist_offset(self) -> None:
        path = self._offset_path
        if path is None:
            return
        await asyncio.to_thread(
            self._write_offset,
            path,
            self._offset,
            self._offset_bot_id,
            self._offset_updated_at,
        )

    @staticmethod
    def _write_offset(
        path: Path,
        offset: int | None,
        bot_id: str,
        updated_at: float,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "bot_id": bot_id,
                    "offset": offset,
                    "updated_at": updated_at,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _offset_is_stale(self) -> bool:
        return self._offset_updated_at <= 0 or self.clock() - self._offset_updated_at > OFFSET_MAX_AGE_S

    @property
    def _offset_path(self) -> Path | None:
        if self.state_dir is None:
            return None
        return self.state_dir / "polling-offset.json"

    @staticmethod
    def _reconnect_delay(failures: int) -> float:
        return min(
            RECONNECT_INITIAL_DELAY_S * (2 ** max(0, failures - 1)),
            RECONNECT_MAX_DELAY_S,
        )

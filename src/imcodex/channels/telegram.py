from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

import httpx

from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy
from .base import BaseChannelAdapter
from .text import split_text


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
TELEGRAM_TEXT_LIMIT = 4000
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
OFFSET_MAX_AGE_S = 6 * 24 * 60 * 60
CONVERSATION_PATTERN = re.compile(r"^chat:(-?\d+)(?::topic:(\d+))?$")


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
        http_client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
        clock=time.time,
    ) -> None:
        super().__init__(
            middleware=middleware,
            access_policy=access_policy or ChannelAccessPolicy(allowed_user_ids=frozenset()),
        )
        self.enabled = enabled
        self.bot_token = bot_token.strip()
        self.bot_token_file = bot_token_file
        self.api_base = api_base.rstrip("/")
        self.require_mention = require_mention
        self.poll_timeout_s = max(1, int(poll_timeout_s))
        self.state_dir = state_dir
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
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
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        self.bot_token = self._resolve_bot_token()
        if not self.bot_token:
            raise RuntimeError(
                "Telegram adapter requires IMCODEX_TELEGRAM_BOT_TOKEN or IMCODEX_TELEGRAM_BOT_TOKEN_FILE when enabled."
            )
        if not self.access_policy.has_allowed_users:
            logger.warning(
                "Telegram has no allowed user IDs; inbound messages will be denied. "
                "Set IMCODEX_TELEGRAM_ALLOWED_USER_IDS."
            )
        self._stop_event.clear()
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_forever())
        mark_channel_health("telegram", enabled=True, connected=False, status="connecting")

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
        if self._owns_http_client:
            try:
                await self.http_client.aclose()
            except Exception as exc:
                errors.append(exc)
        mark_channel_health("telegram", connected=False, status="stopped")
        if errors:
            raise ExceptionGroup("Telegram shutdown failed", errors)

    def parse_inbound_update(self, update: dict[str, Any]) -> tuple[InboundMessage, str] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict) or sender.get("is_bot"):
            return None
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
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
            if not text:
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
        return inbound, message_id

    async def handle_update(self, update: dict[str, Any]) -> None:
        parsed = self.parse_inbound_update(update)
        if parsed is None:
            return
        inbound, reply_to_message_id = parsed
        await self.dispatch_inbound(inbound, reply_to_message_id=reply_to_message_id)

    async def send_message(self, message: OutboundMessage) -> None:
        if not self.enabled or message.channel_id != self.channel_id or not message.text.strip():
            return
        self.ensure_outbound_allowed(message)
        chat_id, thread_id = self._parse_conversation_id(message.conversation_id)
        reply_to = self._parse_reply_message_id(
            message.metadata.get("reply_to_message_id") or message.metadata.get("message_id")
        )
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

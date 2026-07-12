from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlparse


WEIXIN_ACCOUNT_ID_PATTERN = re.compile(r"^[^@\s*]+@im\.bot$")
WEIXIN_USER_ID_PATTERN = re.compile(r"^[^@\s*]+@im\.wechat$")
MAX_WEIXIN_STATE_FILE_BYTES = 2 * 1024 * 1024
MAX_WEIXIN_ID_CHARS = 512
MAX_WEIXIN_TOKEN_CHARS = 16 * 1024
MAX_WEIXIN_CURSOR_CHARS = 256 * 1024
MAX_WEIXIN_CONTEXT_TOKENS = 256


def is_weixin_account_id(value: str) -> bool:
    return WEIXIN_ACCOUNT_ID_PATTERN.fullmatch(value) is not None


def is_weixin_user_id(value: str) -> bool:
    return WEIXIN_USER_ID_PATTERN.fullmatch(value) is not None


def is_official_weixin_base_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    return hostname == "weixin.qq.com" or hostname.endswith(".weixin.qq.com")


@dataclass(frozen=True, slots=True)
class WeixinCredentials:
    account_id: str
    bot_token: str
    base_url: str
    owner_user_id: str = ""
    saved_at: str = ""


@dataclass(slots=True)
class WeixinTransportState:
    account_id: str = ""
    get_updates_buf: str = ""
    context_tokens: dict[str, str] = field(default_factory=dict)

    def set_context_token(self, user_id: str, token: str, *, limit: int = 256) -> None:
        if (
            not is_weixin_user_id(user_id)
            or not token
            or len(user_id) > MAX_WEIXIN_ID_CHARS
            or len(token) > MAX_WEIXIN_TOKEN_CHARS
        ):
            return
        self.context_tokens.pop(user_id, None)
        self.context_tokens[user_id] = token
        overflow = len(self.context_tokens) - min(MAX_WEIXIN_CONTEXT_TOKENS, max(1, limit))
        for old_user_id in list(self.context_tokens)[: max(0, overflow)]:
            self.context_tokens.pop(old_user_id, None)


class WeixinStateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def credentials_path(self) -> Path:
        return self.root / "credentials.json"

    @property
    def transport_state_path(self) -> Path:
        return self.root / "transport.json"

    def load_credentials(self) -> WeixinCredentials | None:
        if not self.credentials_path.exists():
            return None
        self._validate_private_path(self.root, expected_mode=0o700)
        self._validate_private_path(self.credentials_path, expected_mode=0o600)
        payload = self._read_json(self.credentials_path, required=True)
        credential_fields = (
            "account_id",
            "bot_token",
            "base_url",
            "owner_user_id",
            "saved_at",
        )
        if (
            type(payload.get("version")) is not int
            or payload.get("version") != 1
            or any(not isinstance(payload.get(field_name), str) for field_name in credential_fields)
        ):
            raise RuntimeError(f"Invalid Weixin credential file: {self.credentials_path}. Run the login command again.")
        account_id = payload["account_id"].strip()
        bot_token = payload["bot_token"].strip()
        base_url = payload["base_url"].strip()
        owner_user_id = payload["owner_user_id"].strip()
        if (
            not is_weixin_account_id(account_id)
            or not bot_token
            or not is_official_weixin_base_url(base_url)
            or (owner_user_id and not is_weixin_user_id(owner_user_id))
            or len(account_id) > MAX_WEIXIN_ID_CHARS
            or len(owner_user_id) > MAX_WEIXIN_ID_CHARS
            or len(bot_token) > MAX_WEIXIN_TOKEN_CHARS
        ):
            raise RuntimeError(f"Invalid Weixin credential file: {self.credentials_path}. Run the login command again.")
        return WeixinCredentials(
            account_id=account_id,
            bot_token=bot_token,
            base_url=base_url,
            owner_user_id=owner_user_id,
            saved_at=payload["saved_at"].strip(),
        )

    def save_credentials(self, credentials: WeixinCredentials) -> None:
        if (
            not is_weixin_account_id(credentials.account_id)
            or not credentials.bot_token.strip()
            or not is_official_weixin_base_url(credentials.base_url)
            or len(credentials.account_id) > MAX_WEIXIN_ID_CHARS
            or len(credentials.owner_user_id) > MAX_WEIXIN_ID_CHARS
            or len(credentials.bot_token) > MAX_WEIXIN_TOKEN_CHARS
            or (credentials.owner_user_id and not is_weixin_user_id(credentials.owner_user_id))
        ):
            raise RuntimeError("Refusing to persist invalid Weixin credentials.")
        payload = asdict(credentials)
        if not payload["saved_at"]:
            payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        payload["version"] = 1
        self._atomic_write_json(self.credentials_path, payload)

    def load_transport_state(self) -> WeixinTransportState:
        if not self.transport_state_path.exists():
            return WeixinTransportState()
        self._validate_private_path(self.root, expected_mode=0o700)
        self._validate_private_path(self.transport_state_path, expected_mode=0o600)
        try:
            payload = self._read_json(self.transport_state_path, required=False)
        except (OSError, ValueError, json.JSONDecodeError):
            raise RuntimeError(
                f"Could not read Weixin transport state: {self.transport_state_path}. "
                "Run channels logout weixin and log in again to reset it explicitly."
            ) from None
        account_id = payload.get("account_id")
        get_updates_buf = payload.get("get_updates_buf")
        tokens = payload.get("context_tokens")
        if (
            type(payload.get("version")) is not int
            or payload.get("version") != 1
            or not isinstance(account_id, str)
            or not isinstance(get_updates_buf, str)
            or not isinstance(tokens, dict)
            or (account_id and not is_weixin_account_id(account_id))
            or len(account_id) > MAX_WEIXIN_ID_CHARS
            or len(get_updates_buf) > MAX_WEIXIN_CURSOR_CHARS
            or len(tokens) > MAX_WEIXIN_CONTEXT_TOKENS
            or any(
                not isinstance(user_id, str)
                or not isinstance(token, str)
                or not is_weixin_user_id(user_id)
                or not token
                or len(user_id) > MAX_WEIXIN_ID_CHARS
                or len(token) > MAX_WEIXIN_TOKEN_CHARS
                for user_id, token in tokens.items()
            )
        ):
            raise RuntimeError(
                f"Invalid Weixin transport state: {self.transport_state_path}. "
                "Run channels logout weixin and log in again to reset it explicitly."
            )
        return WeixinTransportState(
            account_id=account_id,
            get_updates_buf=get_updates_buf,
            context_tokens=dict(tokens),
        )

    def save_transport_state(self, state: WeixinTransportState) -> None:
        if (
            (state.account_id and not is_weixin_account_id(state.account_id))
            or len(state.account_id) > MAX_WEIXIN_ID_CHARS
            or not isinstance(state.get_updates_buf, str)
            or len(state.get_updates_buf) > MAX_WEIXIN_CURSOR_CHARS
            or len(state.context_tokens) > MAX_WEIXIN_CONTEXT_TOKENS
            or any(
                not is_weixin_user_id(user_id)
                or not token
                or len(user_id) > MAX_WEIXIN_ID_CHARS
                or len(token) > MAX_WEIXIN_TOKEN_CHARS
                for user_id, token in state.context_tokens.items()
            )
        ):
            raise RuntimeError("Refusing to persist invalid Weixin transport state.")
        self._atomic_write_json(
            self.transport_state_path,
            {
                "version": 1,
                "account_id": state.account_id,
                "get_updates_buf": state.get_updates_buf,
                "context_tokens": state.context_tokens,
            },
        )

    def clear(self) -> None:
        for path in (
            self.credentials_path,
            self.transport_state_path,
            self.credentials_path.with_suffix(self.credentials_path.suffix + ".tmp"),
            self.transport_state_path.with_suffix(self.transport_state_path.suffix + ".tmp"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def clear_transport_state(self) -> None:
        for path in (
            self.transport_state_path,
            self.transport_state_path.with_suffix(self.transport_state_path.suffix + ".tmp"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
        try:
            if path.stat().st_size > MAX_WEIXIN_STATE_FILE_BYTES:
                raise ValueError("state file is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            if required:
                raise RuntimeError(f"Could not read Weixin state file: {path}") from None
            raise
        if not isinstance(payload, dict):
            if required:
                raise RuntimeError(f"Invalid Weixin state file: {path}")
            raise ValueError("state payload must be an object")
        return payload

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        if self.root.is_symlink():
            raise RuntimeError(f"Weixin state path must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod(self.root, 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
            self._chmod(temporary, 0o600)
            os.replace(temporary, path)
            self._chmod(path, 0o600)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        if os.name == "nt":
            return
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise RuntimeError(f"Could not secure Weixin state path {path} to {mode:o}") from exc

    @staticmethod
    def _validate_private_path(path: Path, *, expected_mode: int) -> None:
        if path.is_symlink():
            raise RuntimeError(f"Weixin state path must not be a symlink: {path}")
        if os.name == "nt":
            return
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"Could not inspect Weixin state path: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"Weixin state path must not be a symlink: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise RuntimeError(f"Insecure Weixin state permissions on {path}: {mode:o}; expected {expected_mode:o}.")

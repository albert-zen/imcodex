"""Explicit, UI-safe schema for bridge-owned environment settings."""

from __future__ import annotations

import math
import ipaddress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from ..app_server_target import default_app_server_endpoint, parse_app_server_target
from ..config import validate_http_endpoint


FieldKind = Literal["string", "boolean", "integer", "number", "select", "secret"]


class FieldValueError(ValueError):
    """Raised when a field value cannot be represented safely."""


def _validate_text(value: str, *, key: str, max_length: int) -> None:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise FieldValueError(f"{key} must be a single line without NUL characters")
    if len(value) > max_length:
        raise FieldValueError(f"{key} must be at most {max_length} characters")


def _validate_http_url(value: str, *, key: str) -> None:
    if not value:
        return
    try:
        validate_http_endpoint(value, key=key)
    except ValueError as exc:
        raise FieldValueError(str(exc)) from exc


def _validate_outbound_url(value: str, *, key: str) -> None:
    _validate_http_url(value, key=key)
    if not value:
        return
    parsed = urlsplit(value)
    host = str(parsed.hostname or "").rstrip(".").lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and parsed.scheme.lower() != "https":
        raise FieldValueError(f"{key} requires HTTPS for remote hosts")


@dataclass(frozen=True, slots=True)
class ConfigFieldDefinition:
    key: str
    section: str
    label: str
    kind: FieldKind
    default: object = ""
    description: str = ""
    options: tuple[str, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    max_length: int = 4096
    aliases: tuple[str, ...] = ()
    environment_group: tuple[str, ...] = ()
    validation: Literal["none", "http_url", "outbound_url", "app_server_url"] = "none"

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    @property
    def storage_keys(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)

    @property
    def process_names(self) -> tuple[str, ...]:
        return self.environment_group or self.storage_keys

    def validate(self, value: object, *, secret_replacement: bool = False) -> str:
        if self.secret and not secret_replacement:
            raise FieldValueError(f"{self.key} must be updated through the secrets payload")

        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise FieldValueError(f"{self.key} must be a boolean")
            return "1" if value else "0"

        if self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FieldValueError(f"{self.key} must be an integer")
            self._validate_range(value)
            return str(value)

        if self.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FieldValueError(f"{self.key} must be a number")
            try:
                numeric = float(value)
            except (OverflowError, ValueError) as exc:
                raise FieldValueError(f"{self.key} must be finite") from exc
            if not math.isfinite(numeric):
                raise FieldValueError(f"{self.key} must be finite")
            self._validate_range(numeric)
            return str(value)

        if not isinstance(value, str):
            raise FieldValueError(f"{self.key} must be a string")
        _validate_text(value, key=self.key, max_length=self.max_length)
        if self.kind == "select" and value not in self.options:
            choices = ", ".join(self.options)
            raise FieldValueError(f"{self.key} must be one of: {choices}")
        if self.secret and not value:
            raise FieldValueError(f"{self.key} replacement must not be empty; use clear instead")
        if self.validation == "http_url":
            _validate_http_url(value, key=self.key)
        elif self.validation == "outbound_url":
            _validate_outbound_url(value, key=self.key)
        elif self.validation == "app_server_url" and value:
            try:
                parse_app_server_target(value)
            except ValueError as exc:
                raise FieldValueError(str(exc)) from exc
        return value

    def parse(self, raw: str) -> object:
        if self.secret:
            raise FieldValueError(f"{self.key} is secret and cannot be read")
        if self.kind == "boolean":
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if self.kind == "integer":
            try:
                return int(raw)
            except ValueError:
                return raw
        if self.kind == "number":
            try:
                return float(raw)
            except ValueError:
                return raw
        return raw

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "key": self.key,
            "section": self.section,
            "label": self.label,
            "type": self.kind,
            "description": self.description,
        }
        if not self.secret:
            result["default"] = self.default
        if self.options:
            result["options"] = list(self.options)
        if self.kind in {"string", "secret"}:
            result["max_length"] = self.max_length
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        return result

    def _validate_range(self, value: int | float) -> None:
        if self.minimum is not None and value < self.minimum:
            raise FieldValueError(f"{self.key} must be at least {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise FieldValueError(f"{self.key} must be at most {self.maximum}")


def _field(
    key: str,
    section: str,
    label: str,
    kind: FieldKind = "string",
    default: object = "",
    **kwargs: object,
) -> ConfigFieldDefinition:
    return ConfigFieldDefinition(key, section, label, kind, default, **kwargs)


_TARGET_ENVIRONMENT_GROUP = (
    "IMCODEX_APP_SERVER_URL",
    "IMCODEX_CORE_URL",
    "IMCODEX_CORE_MODE",
    "IMCODEX_CORE_PORT",
)


CONFIG_FIELDS: tuple[ConfigFieldDefinition, ...] = (
    _field("IMCODEX_DATA_DIR", "runtime", "Data directory", default=".imcodex"),
    _field("IMCODEX_CODEX_BIN", "runtime", "Codex executable", default="codex"),
    _field(
        "IMCODEX_LOG_LEVEL",
        "runtime",
        "Log level",
        "select",
        "INFO",
        options=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    _field(
        "IMCODEX_HTTP_HOST",
        "runtime",
        "HTTP host",
        default="0.0.0.0",
        description="The admin page remains loopback-only even when other HTTP routes listen on all interfaces.",
    ),
    _field(
        "IMCODEX_HTTP_PORT",
        "runtime",
        "HTTP port",
        "integer",
        8000,
        minimum=1,
        maximum=65535,
    ),
    _field(
        "IMCODEX_SERVICE_NAME",
        "runtime",
        "Service name",
        default="imcodex",
        max_length=256,
    ),
    _field(
        "IMCODEX_APP_SERVER_URL",
        "app_server",
        "App Server URL",
        default=default_app_server_endpoint(),
        description=(
            "Use unix://, ws://, or wss:// for an independently owned App Server; "
            "stdio:// makes the bridge own a child App Server process."
        ),
        validation="app_server_url",
        aliases=("IMCODEX_CORE_URL", "IMCODEX_CORE_MODE", "IMCODEX_CORE_PORT"),
        environment_group=_TARGET_ENVIRONMENT_GROUP,
    ),
    _field(
        "IMCODEX_APP_SERVER_EXPERIMENTAL_API",
        "app_server",
        "Experimental API",
        "boolean",
        False,
    ),
    _field(
        "IMCODEX_APP_SERVER_AUTH_TOKEN_FILE",
        "app_server",
        "Authorization token file",
        description="Preferred way to supply a private App Server bearer token without storing it in .env.",
    ),
    _field(
        "IMCODEX_APP_SERVER_AUTH_TOKEN",
        "app_server",
        "Authorization token",
        "secret",
        None,
        max_length=16384,
    ),
    _field(
        "IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS",
        "app_server",
        "Connect attempts",
        "integer",
        3,
        minimum=1,
        maximum=100,
    ),
    _field(
        "IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS",
        "app_server",
        "Request attempts",
        "integer",
        3,
        minimum=1,
        maximum=100,
    ),
    _field(
        "IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY",
        "app_server",
        "Retry initial delay",
        "number",
        0.25,
        minimum=0.01,
        maximum=300,
    ),
    _field(
        "IMCODEX_APP_SERVER_RETRY_MAX_DELAY",
        "app_server",
        "Retry maximum delay",
        "number",
        2.0,
        minimum=0.01,
        maximum=3600,
    ),
    _field(
        "IMCODEX_APP_SERVER_RETRY_JITTER",
        "app_server",
        "Retry jitter",
        "number",
        0.25,
        minimum=0,
        maximum=1,
    ),
    _field(
        "IMCODEX_APP_SERVER_CONNECT_TIMEOUT",
        "app_server",
        "Connect timeout",
        "number",
        3.0,
        minimum=0.1,
        maximum=300,
    ),
    _field(
        "IMCODEX_APP_SERVER_HEALTH_TIMEOUT",
        "app_server",
        "Health timeout",
        "number",
        1.0,
        minimum=0.1,
        maximum=300,
    ),
    _field(
        "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY",
        "app_server",
        "Reconnect initial delay",
        "number",
        0.5,
        minimum=0.01,
        maximum=3600,
    ),
    _field(
        "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY",
        "app_server",
        "Reconnect maximum delay",
        "number",
        30.0,
        minimum=0.01,
        maximum=86400,
    ),
    _field(
        "IMCODEX_APP_SERVER_RECONNECT_JITTER",
        "app_server",
        "Reconnect jitter",
        "number",
        0.25,
        minimum=0,
        maximum=1,
    ),
    _field(
        "IMCODEX_OUTBOUND_URL",
        "webhooks",
        "Outbound URL",
        description="Remote destinations require HTTPS and an outbound bearer token.",
        validation="outbound_url",
        max_length=2048,
    ),
    _field(
        "IMCODEX_OUTBOUND_WEBHOOK_TOKEN",
        "webhooks",
        "Outbound bearer token",
        "secret",
        None,
        max_length=16384,
    ),
    _field(
        "IMCODEX_INBOUND_WEBHOOK_TOKEN",
        "webhooks",
        "Inbound bearer token",
        "secret",
        None,
        max_length=16384,
    ),
    _field(
        "IMCODEX_QQ_ENABLED",
        "qq",
        "Enabled",
        "boolean",
        False,
        description="Requires an App ID and client secret. An empty user allowlist still denies every inbound message.",
    ),
    _field("IMCODEX_QQ_APP_ID", "qq", "App ID"),
    _field(
        "IMCODEX_QQ_CLIENT_SECRET",
        "qq",
        "Client secret",
        "secret",
        None,
        max_length=16384,
    ),
    _field(
        "IMCODEX_QQ_API_BASE",
        "qq",
        "API base",
        default="https://api.sgroup.qq.com",
        validation="http_url",
        max_length=2048,
    ),
    _field("IMCODEX_QQ_MARKDOWN_ENABLED", "qq", "Markdown enabled", "boolean", True),
    _field(
        "IMCODEX_QQ_ALLOWED_USER_IDS",
        "qq",
        "Allowed user IDs",
        description="Comma-separated stable IDs. Empty denies all users; * explicitly allows all users.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_QQ_ALLOWED_CONVERSATION_IDS",
        "qq",
        "Allowed conversations",
        description="Optional comma-separated conversation IDs. Empty allows every conversation for an allowed user.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_TELEGRAM_ENABLED",
        "telegram",
        "Enabled",
        "boolean",
        False,
        description="Requires a bot token or private token file. An empty user allowlist denies every inbound message.",
    ),
    _field(
        "IMCODEX_TELEGRAM_BOT_TOKEN_FILE",
        "telegram",
        "Bot token file",
        description="Path to a non-symlink private file (0600 on POSIX) containing the bot token.",
    ),
    _field(
        "IMCODEX_TELEGRAM_BOT_TOKEN",
        "telegram",
        "Bot token",
        "secret",
        None,
        max_length=16384,
    ),
    _field(
        "IMCODEX_TELEGRAM_API_BASE",
        "telegram",
        "API base",
        default="https://api.telegram.org",
        validation="http_url",
        max_length=2048,
    ),
    _field(
        "IMCODEX_TELEGRAM_ALLOWED_USER_IDS",
        "telegram",
        "Allowed user IDs",
        description="Comma-separated stable IDs. Empty denies all users; * explicitly allows all users.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_TELEGRAM_ALLOWED_CONVERSATION_IDS",
        "telegram",
        "Allowed conversations",
        description="Optional comma-separated chat or topic IDs. Empty allows every conversation for an allowed user.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_TELEGRAM_REQUIRE_MENTION",
        "telegram",
        "Require mention",
        "boolean",
        True,
    ),
    _field(
        "IMCODEX_TELEGRAM_POLL_TIMEOUT",
        "telegram",
        "Polling timeout",
        "integer",
        30,
        minimum=1,
        maximum=120,
    ),
    _field(
        "IMCODEX_FEISHU_ENABLED",
        "feishu",
        "Enabled",
        "boolean",
        False,
        description="Requires the Feishu extra plus an App ID and app secret. An empty user allowlist denies every inbound message.",
    ),
    _field("IMCODEX_FEISHU_APP_ID", "feishu", "App ID", aliases=("IMCODEX_LARK_APP_ID",)),
    _field(
        "IMCODEX_FEISHU_APP_SECRET",
        "feishu",
        "App secret",
        "secret",
        None,
        max_length=16384,
        aliases=("IMCODEX_LARK_APP_SECRET",),
    ),
    _field(
        "IMCODEX_FEISHU_DOMAIN",
        "feishu",
        "Service",
        "select",
        "feishu",
        options=("feishu", "lark"),
    ),
    _field(
        "IMCODEX_FEISHU_ALLOWED_USER_IDS",
        "feishu",
        "Allowed user IDs",
        description="Comma-separated stable IDs. Empty denies all users; * explicitly allows all users.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_FEISHU_ALLOWED_CONVERSATION_IDS",
        "feishu",
        "Allowed conversations",
        description="Optional comma-separated chat IDs. Empty allows every conversation for an allowed user.",
        max_length=8192,
    ),
    _field("IMCODEX_FEISHU_REQUIRE_MENTION", "feishu", "Require mention", "boolean", True),
    _field(
        "IMCODEX_FEISHU_STARTUP_TIMEOUT",
        "feishu",
        "Startup timeout",
        "number",
        30.0,
        minimum=1,
        maximum=300,
    ),
    _field(
        "IMCODEX_WEIXIN_ENABLED",
        "weixin",
        "Enabled",
        "boolean",
        False,
        description="Requires a saved Weixin login. The logged-in owner is admitted automatically when no user IDs are listed.",
    ),
    _field(
        "IMCODEX_WEIXIN_STATE_DIR",
        "weixin",
        "State directory",
        description="Leave empty to use the channel state under the main IMCodex data directory.",
    ),
    _field(
        "IMCODEX_WEIXIN_ALLOWED_USER_IDS",
        "weixin",
        "Allowed user IDs",
        description="Optional comma-separated IDs added to the logged-in owner.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_WEIXIN_ALLOWED_CONVERSATION_IDS",
        "weixin",
        "Allowed conversations",
        description="Optional comma-separated conversation IDs. Empty allows every conversation for an allowed user.",
        max_length=8192,
    ),
    _field(
        "IMCODEX_WEIXIN_POLL_TIMEOUT_MS",
        "weixin",
        "Polling timeout",
        "integer",
        35000,
        minimum=5000,
        maximum=120000,
    ),
)

CONFIG_FIELDS_BY_KEY = MappingProxyType({field.key: field for field in CONFIG_FIELDS})

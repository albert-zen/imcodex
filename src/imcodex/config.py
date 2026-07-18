from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .app_server_target import AppServerTarget, resolve_app_server_target


DOTENV_IMPORTED_KEYS_ENV = "IMCODEX_DOTENV_IMPORTED_KEYS"
LAUNCHER_RELOADABLE_KEYS_ENV = "IMCODEX_LAUNCHER_RELOADABLE_KEYS"
MANAGED_APP_SERVER_TARGET_ENV = "IMCODEX_INTERNAL_MANAGED_APP_SERVER_TARGET"
PREFLIGHT_CURRENT_HTTP_HOST_ENV = "IMCODEX_INTERNAL_PREFLIGHT_CURRENT_HTTP_HOST"
PREFLIGHT_CURRENT_HTTP_PORT_ENV = "IMCODEX_INTERNAL_PREFLIGHT_CURRENT_HTTP_PORT"
RESTART_CONTEXT_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "USERPROFILE",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
RESTART_CONTEXT_ENV_PREFIXES = ("CODEX_", "OPENAI_")
TARGET_ENVIRONMENT_KEYS = frozenset(
    {
        "IMCODEX_APP_SERVER_URL",
        "IMCODEX_CORE_URL",
        "IMCODEX_CORE_MODE",
        "IMCODEX_CORE_PORT",
    }
)
KNOWN_SETTING_ENV_KEYS = frozenset(
    {
        "IMCODEX_APP_SERVER_AUTH_TOKEN",
        "IMCODEX_APP_SERVER_AUTH_TOKEN_FILE",
        "IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS",
        "IMCODEX_APP_SERVER_CONNECT_TIMEOUT",
        "IMCODEX_APP_SERVER_EXPERIMENTAL_API",
        "IMCODEX_APP_SERVER_HEALTH_TIMEOUT",
        MANAGED_APP_SERVER_TARGET_ENV,
        "IMCODEX_NATIVE_THREAD_TOOL_HOST",
        "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY",
        "IMCODEX_APP_SERVER_RECONNECT_JITTER",
        "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY",
        "IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS",
        "IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY",
        "IMCODEX_APP_SERVER_RETRY_JITTER",
        "IMCODEX_APP_SERVER_RETRY_MAX_DELAY",
        "IMCODEX_APP_SERVER_URL",
        "IMCODEX_CODEX_BIN",
        "IMCODEX_CORE_MODE",
        "IMCODEX_CORE_PORT",
        "IMCODEX_CORE_URL",
        "IMCODEX_DATA_DIR",
        "IMCODEX_DEBUG_API_ENABLED",
        "IMCODEX_FEISHU_ALLOWED_CONVERSATION_IDS",
        "IMCODEX_FEISHU_ALLOWED_USER_IDS",
        "IMCODEX_FEISHU_ACCESS_MATCH",
        "IMCODEX_FEISHU_APP_ID",
        "IMCODEX_FEISHU_APP_SECRET",
        "IMCODEX_FEISHU_DOMAIN",
        "IMCODEX_FEISHU_ENABLED",
        "IMCODEX_FEISHU_REQUIRE_MENTION",
        "IMCODEX_FEISHU_STARTUP_TIMEOUT",
        "IMCODEX_HTTP_HOST",
        "IMCODEX_HTTP_PORT",
        "IMCODEX_INBOUND_WEBHOOK_TOKEN",
        "IMCODEX_LARK_APP_ID",
        "IMCODEX_LARK_APP_SECRET",
        "IMCODEX_LOG_LEVEL",
        "IMCODEX_OUTBOUND_URL",
        "IMCODEX_OUTBOUND_WEBHOOK_TOKEN",
        "IMCODEX_QQ_ALLOWED_CONVERSATION_IDS",
        "IMCODEX_QQ_ALLOWED_USER_IDS",
        "IMCODEX_QQ_ACCESS_MATCH",
        "IMCODEX_QQ_API_BASE",
        "IMCODEX_QQ_APP_ID",
        "IMCODEX_QQ_CLIENT_SECRET",
        "IMCODEX_QQ_ENABLED",
        "IMCODEX_QQ_MARKDOWN_ENABLED",
        "IMCODEX_RESTART_EXECUTOR",
        "IMCODEX_RUN_DIR",
        "IMCODEX_SERVICE_NAME",
        "IMCODEX_TELEGRAM_ALLOWED_CONVERSATION_IDS",
        "IMCODEX_TELEGRAM_ALLOWED_USER_IDS",
        "IMCODEX_TELEGRAM_ACCESS_MATCH",
        "IMCODEX_TELEGRAM_API_BASE",
        "IMCODEX_TELEGRAM_BOT_TOKEN",
        "IMCODEX_TELEGRAM_BOT_TOKEN_FILE",
        "IMCODEX_TELEGRAM_ENABLED",
        "IMCODEX_TELEGRAM_POLL_TIMEOUT",
        "IMCODEX_TELEGRAM_REQUIRE_MENTION",
        "IMCODEX_WEIXIN_ALLOWED_CONVERSATION_IDS",
        "IMCODEX_WEIXIN_ALLOWED_USER_IDS",
        "IMCODEX_WEIXIN_ACCESS_MATCH",
        "IMCODEX_WEIXIN_ENABLED",
        "IMCODEX_WEIXIN_POLL_TIMEOUT_MS",
        "IMCODEX_WEIXIN_STATE_DIR",
    }
)


def is_restart_context_env_key(key: str) -> bool:
    return key in RESTART_CONTEXT_ENV_KEYS or key.startswith(RESTART_CONTEXT_ENV_PREFIXES)


def validate_http_endpoint(value: str, *, key: str) -> None:
    """Validate a non-empty HTTP(S) base URL without embedded credentials."""

    if any(character.isspace() for character in value):
        raise ValueError(f"{key} must not contain whitespace")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{key} must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not host:
        raise ValueError(f"{key} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{key} must not contain userinfo credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{key} must not contain query or fragment credentials")


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _env(name: str, default: str, dotenv: dict[str, str]) -> str:
    return os.getenv(name, dotenv.get(name, default))


def _env_with_fallback(
    name: str,
    fallback_name: str,
    dotenv: dict[str, str],
) -> str:
    value = _env(name, "", dotenv)
    if value.strip():
        return value
    return _env(fallback_name, "", dotenv)


def _env_bool(name: str, default: bool, dotenv: dict[str, str]) -> bool:
    raw = os.getenv(name, dotenv.get(name))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _process_env_optional(name: str) -> str | None:
    """Read a launcher-owned capability without accepting a .env assertion."""

    return _optional_setting(os.getenv(name))


def _env_int(name: str, default: int, dotenv: dict[str, str]) -> int:
    return int(_env(name, str(default), dotenv))


def _env_float(name: str, default: float, dotenv: dict[str, str]) -> float:
    return float(_env(name, str(default), dotenv))


def _codex_bin(dotenv: dict[str, str]) -> str:
    return _env("IMCODEX_CODEX_BIN", "codex", dotenv)


def load_codex_bin(dotenv_path: Path = Path(".env")) -> str:
    return _codex_bin(_read_dotenv(dotenv_path))


def _optional_setting(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _app_server_config_from_env(
    dotenv: dict[str, str],
) -> tuple[str | None, str | None, str | None]:
    names = (
        "IMCODEX_APP_SERVER_URL",
        "IMCODEX_CORE_URL",
        "IMCODEX_CORE_MODE",
        "IMCODEX_CORE_PORT",
    )
    process_values = tuple(_optional_setting(os.getenv(name)) for name in names)
    managed_target = _process_env_optional(MANAGED_APP_SERVER_TARGET_ENV)
    if any(value is not None for value in process_values):
        values = process_values
    elif managed_target is not None:
        values = (managed_target, None, None, None)
    else:
        values = tuple(_optional_setting(dotenv.get(name)) for name in names)
    app_server_url, core_url, core_mode, core_port = values
    if core_port is not None and app_server_url is None and core_url is None:
        try:
            port = int(core_port)
        except ValueError as exc:
            raise ValueError("IMCODEX_CORE_PORT must be an integer between 1 and 65535") from exc
        if not 1 <= port <= 65535:
            raise ValueError("IMCODEX_CORE_PORT must be an integer between 1 and 65535")
        core_url = f"ws://127.0.0.1:{port}"
        core_mode = core_mode or "dedicated-ws"
    return app_server_url, core_url, core_mode


def load_app_server_target(dotenv_path: Path = Path(".env")) -> AppServerTarget:
    app_server_url, core_url, core_mode = _app_server_config_from_env(_read_dotenv(dotenv_path))
    return resolve_app_server_target(
        app_server_url=app_server_url,
        core_url=core_url,
        core_mode=core_mode,
    )


@dataclass(slots=True)
class Settings:
    data_dir: Path
    run_dir: Path
    codex_bin: str
    app_server_url: str | None
    app_server_experimental_api_enabled: bool
    core_mode: str | None
    core_url: str | None
    restart_executor: str | None
    debug_api_enabled: bool
    log_level: str
    http_host: str
    http_port: int
    outbound_url: str | None
    service_name: str
    qq_enabled: bool
    qq_app_id: str
    qq_client_secret: str
    qq_api_base: str
    qq_markdown_enabled: bool
    app_server_auth_token: str | None = None
    app_server_auth_token_file: Path | None = None
    app_server_connect_max_attempts: int = 3
    app_server_request_max_attempts: int = 3
    app_server_retry_initial_delay_s: float = 0.25
    app_server_retry_max_delay_s: float = 2.0
    app_server_retry_jitter_fraction: float = 0.25
    app_server_connect_timeout_s: float = 3.0
    app_server_health_timeout_s: float = 1.0
    native_thread_tool_host: bool = False
    app_server_managed_target: str | None = None
    app_server_reconnect_initial_delay_s: float = 0.5
    app_server_reconnect_max_delay_s: float = 30.0
    app_server_reconnect_jitter_fraction: float = 0.25
    qq_allowed_user_ids: str = ""
    qq_allowed_conversation_ids: str = ""
    qq_access_match: str = "any"
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_bot_token_file: Path | None = None
    telegram_api_base: str = "https://api.telegram.org"
    telegram_allowed_user_ids: str = ""
    telegram_allowed_conversation_ids: str = ""
    telegram_access_match: str = "any"
    telegram_require_mention: bool = True
    telegram_poll_timeout_s: int = 30
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "feishu"
    feishu_allowed_user_ids: str = ""
    feishu_allowed_conversation_ids: str = ""
    feishu_access_match: str = "any"
    feishu_require_mention: bool = True
    feishu_startup_timeout_s: float = 30.0
    weixin_enabled: bool = False
    weixin_state_dir: Path | None = None
    weixin_allowed_user_ids: str = ""
    weixin_allowed_conversation_ids: str = ""
    weixin_access_match: str = "any"
    weixin_poll_timeout_ms: int = 35_000
    outbound_webhook_token: str = ""
    inbound_webhook_token: str = ""

    def __post_init__(self) -> None:
        resolve_app_server_target(
            app_server_url=self.app_server_url,
            core_url=self.core_url,
            core_mode=self.core_mode,
        )
        if self.app_server_reconnect_initial_delay_s <= 0:
            raise ValueError("app-server reconnect initial delay must be greater than zero")
        if self.app_server_reconnect_max_delay_s < self.app_server_reconnect_initial_delay_s:
            raise ValueError("app-server reconnect max delay must be at least the initial delay")
        if not 0 <= self.app_server_reconnect_jitter_fraction <= 1:
            raise ValueError("app-server reconnect jitter must be between zero and one")

    def channel_configs(self) -> dict[str, dict[str, object]]:
        weixin_state_dir = self.weixin_state_dir or self.data_dir / "channels" / "weixin"
        return {
            "qq": {
                "enabled": self.qq_enabled,
                "app_id": self.qq_app_id,
                "client_secret": self.qq_client_secret,
                "api_base": self.qq_api_base,
                "media_dir": self.data_dir / "channels" / "qq" / "inbound-media",
                "markdown_enabled": self.qq_markdown_enabled,
                "allowed_user_ids": self.qq_allowed_user_ids,
                "allowed_conversation_ids": self.qq_allowed_conversation_ids,
                "access_match": self.qq_access_match,
            },
            "telegram": {
                "enabled": self.telegram_enabled,
                "bot_token": self.telegram_bot_token,
                "bot_token_file": self.telegram_bot_token_file,
                "api_base": self.telegram_api_base,
                "allowed_user_ids": self.telegram_allowed_user_ids,
                "allowed_conversation_ids": self.telegram_allowed_conversation_ids,
                "access_match": self.telegram_access_match,
                "require_mention": self.telegram_require_mention,
                "poll_timeout_s": self.telegram_poll_timeout_s,
                "state_dir": self.data_dir / "channels" / "telegram",
                "media_dir": self.data_dir / "channels" / "telegram" / "inbound-media",
            },
            "feishu": {
                "enabled": self.feishu_enabled,
                "app_id": self.feishu_app_id,
                "app_secret": self.feishu_app_secret,
                "domain": self.feishu_domain,
                "allowed_user_ids": self.feishu_allowed_user_ids,
                "allowed_conversation_ids": self.feishu_allowed_conversation_ids,
                "access_match": self.feishu_access_match,
                "require_mention": self.feishu_require_mention,
                "startup_timeout_s": self.feishu_startup_timeout_s,
                "media_dir": self.data_dir / "channels" / "feishu" / "inbound-media",
            },
            "weixin": {
                "enabled": self.weixin_enabled,
                "state_dir": weixin_state_dir,
                "media_dir": weixin_state_dir / "inbound-media",
                "allowed_user_ids": self.weixin_allowed_user_ids,
                "allowed_conversation_ids": self.weixin_allowed_conversation_ids,
                "access_match": self.weixin_access_match,
                "poll_timeout_ms": self.weixin_poll_timeout_ms,
            },
        }

    @property
    def app_server_target(self) -> AppServerTarget:
        return resolve_app_server_target(
            app_server_url=self.app_server_url,
            core_url=self.core_url,
            core_mode=self.core_mode,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        dotenv = _read_dotenv(Path(".env"))
        app_server_url, core_url, core_mode = _app_server_config_from_env(dotenv)
        return cls(
            data_dir=Path(_env("IMCODEX_DATA_DIR", ".imcodex", dotenv)),
            run_dir=Path(_env("IMCODEX_RUN_DIR", ".imcodex-run", dotenv)),
            codex_bin=_codex_bin(dotenv),
            app_server_url=app_server_url,
            app_server_experimental_api_enabled=_env_bool("IMCODEX_APP_SERVER_EXPERIMENTAL_API", False, dotenv),
            core_mode=core_mode,
            core_url=core_url,
            restart_executor=_env("IMCODEX_RESTART_EXECUTOR", "", dotenv) or None,
            debug_api_enabled=_env_bool("IMCODEX_DEBUG_API_ENABLED", False, dotenv),
            log_level=_env("IMCODEX_LOG_LEVEL", "INFO", dotenv),
            http_host=_env("IMCODEX_HTTP_HOST", "0.0.0.0", dotenv),
            http_port=int(_env("IMCODEX_HTTP_PORT", "8000", dotenv)),
            outbound_url=_env("IMCODEX_OUTBOUND_URL", "", dotenv) or None,
            service_name=_env("IMCODEX_SERVICE_NAME", "imcodex", dotenv),
            qq_enabled=_env_bool("IMCODEX_QQ_ENABLED", False, dotenv),
            qq_app_id=_env("IMCODEX_QQ_APP_ID", "", dotenv),
            qq_client_secret=_env("IMCODEX_QQ_CLIENT_SECRET", "", dotenv),
            qq_api_base=_env("IMCODEX_QQ_API_BASE", "https://api.sgroup.qq.com", dotenv),
            qq_markdown_enabled=_env_bool("IMCODEX_QQ_MARKDOWN_ENABLED", True, dotenv),
            app_server_auth_token=_env("IMCODEX_APP_SERVER_AUTH_TOKEN", "", dotenv).strip() or None,
            app_server_auth_token_file=(
                Path(path) if (path := _env("IMCODEX_APP_SERVER_AUTH_TOKEN_FILE", "", dotenv).strip()) else None
            ),
            app_server_connect_max_attempts=_env_int("IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS", 3, dotenv),
            app_server_request_max_attempts=_env_int("IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS", 3, dotenv),
            app_server_retry_initial_delay_s=_env_float("IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY", 0.25, dotenv),
            app_server_retry_max_delay_s=_env_float("IMCODEX_APP_SERVER_RETRY_MAX_DELAY", 2.0, dotenv),
            app_server_retry_jitter_fraction=_env_float("IMCODEX_APP_SERVER_RETRY_JITTER", 0.25, dotenv),
            app_server_connect_timeout_s=_env_float("IMCODEX_APP_SERVER_CONNECT_TIMEOUT", 3.0, dotenv),
            app_server_health_timeout_s=_env_float("IMCODEX_APP_SERVER_HEALTH_TIMEOUT", 1.0, dotenv),
            native_thread_tool_host=_env_bool("IMCODEX_NATIVE_THREAD_TOOL_HOST", False, dotenv),
            app_server_managed_target=_process_env_optional(MANAGED_APP_SERVER_TARGET_ENV),
            app_server_reconnect_initial_delay_s=_env_float(
                "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY",
                0.5,
                dotenv,
            ),
            app_server_reconnect_max_delay_s=_env_float(
                "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY",
                30.0,
                dotenv,
            ),
            app_server_reconnect_jitter_fraction=_env_float(
                "IMCODEX_APP_SERVER_RECONNECT_JITTER",
                0.25,
                dotenv,
            ),
            qq_allowed_user_ids=_env("IMCODEX_QQ_ALLOWED_USER_IDS", "", dotenv),
            qq_allowed_conversation_ids=_env("IMCODEX_QQ_ALLOWED_CONVERSATION_IDS", "", dotenv),
            qq_access_match=_env("IMCODEX_QQ_ACCESS_MATCH", "any", dotenv),
            telegram_enabled=_env_bool("IMCODEX_TELEGRAM_ENABLED", False, dotenv),
            telegram_bot_token=_env("IMCODEX_TELEGRAM_BOT_TOKEN", "", dotenv),
            telegram_bot_token_file=(
                Path(path) if (path := _env("IMCODEX_TELEGRAM_BOT_TOKEN_FILE", "", dotenv).strip()) else None
            ),
            telegram_api_base=_env("IMCODEX_TELEGRAM_API_BASE", "https://api.telegram.org", dotenv),
            telegram_allowed_user_ids=_env("IMCODEX_TELEGRAM_ALLOWED_USER_IDS", "", dotenv),
            telegram_allowed_conversation_ids=_env("IMCODEX_TELEGRAM_ALLOWED_CONVERSATION_IDS", "", dotenv),
            telegram_access_match=_env("IMCODEX_TELEGRAM_ACCESS_MATCH", "any", dotenv),
            telegram_require_mention=_env_bool("IMCODEX_TELEGRAM_REQUIRE_MENTION", True, dotenv),
            telegram_poll_timeout_s=_env_int("IMCODEX_TELEGRAM_POLL_TIMEOUT", 30, dotenv),
            feishu_enabled=_env_bool("IMCODEX_FEISHU_ENABLED", False, dotenv),
            feishu_app_id=_env_with_fallback(
                "IMCODEX_FEISHU_APP_ID",
                "IMCODEX_LARK_APP_ID",
                dotenv,
            ),
            feishu_app_secret=_env_with_fallback(
                "IMCODEX_FEISHU_APP_SECRET",
                "IMCODEX_LARK_APP_SECRET",
                dotenv,
            ),
            feishu_domain=_env("IMCODEX_FEISHU_DOMAIN", "feishu", dotenv),
            feishu_allowed_user_ids=_env("IMCODEX_FEISHU_ALLOWED_USER_IDS", "", dotenv),
            feishu_allowed_conversation_ids=_env("IMCODEX_FEISHU_ALLOWED_CONVERSATION_IDS", "", dotenv),
            feishu_access_match=_env("IMCODEX_FEISHU_ACCESS_MATCH", "any", dotenv),
            feishu_require_mention=_env_bool("IMCODEX_FEISHU_REQUIRE_MENTION", True, dotenv),
            feishu_startup_timeout_s=_env_float("IMCODEX_FEISHU_STARTUP_TIMEOUT", 30.0, dotenv),
            weixin_enabled=_env_bool("IMCODEX_WEIXIN_ENABLED", False, dotenv),
            weixin_state_dir=(Path(path) if (path := _env("IMCODEX_WEIXIN_STATE_DIR", "", dotenv).strip()) else None),
            weixin_allowed_user_ids=_env("IMCODEX_WEIXIN_ALLOWED_USER_IDS", "", dotenv),
            weixin_allowed_conversation_ids=_env("IMCODEX_WEIXIN_ALLOWED_CONVERSATION_IDS", "", dotenv),
            weixin_access_match=_env("IMCODEX_WEIXIN_ACCESS_MATCH", "any", dotenv),
            weixin_poll_timeout_ms=_env_int("IMCODEX_WEIXIN_POLL_TIMEOUT_MS", 35_000, dotenv),
            outbound_webhook_token=_env("IMCODEX_OUTBOUND_WEBHOOK_TOKEN", "", dotenv),
            inbound_webhook_token=_env("IMCODEX_INBOUND_WEBHOOK_TOKEN", "", dotenv),
        )

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


def _env_bool(name: str, default: bool, dotenv: dict[str, str]) -> bool:
    raw = os.getenv(name, dotenv.get(name))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, dotenv: dict[str, str]) -> int:
    return int(_env(name, str(default), dotenv))


def _env_float(name: str, default: float, dotenv: dict[str, str]) -> float:
    return float(_env(name, str(default), dotenv))


@dataclass(slots=True)
class Settings:
    data_dir: Path
    run_dir: Path
    codex_bin: str
    app_server_url: str | None
    app_server_experimental_api_enabled: bool
    core_mode: str
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

    def channel_configs(self) -> dict[str, dict[str, object]]:
        return {
            "qq": {
                "enabled": self.qq_enabled,
                "app_id": self.qq_app_id,
                "client_secret": self.qq_client_secret,
                "api_base": self.qq_api_base,
                "markdown_enabled": self.qq_markdown_enabled,
            }
        }

    @classmethod
    def from_env(cls) -> "Settings":
        dotenv = _read_dotenv(Path(".env"))
        return cls(
            data_dir=Path(_env("IMCODEX_DATA_DIR", ".imcodex", dotenv)),
            run_dir=Path(_env("IMCODEX_RUN_DIR", ".imcodex-run", dotenv)),
            codex_bin=_env("IMCODEX_CODEX_BIN", "codex", dotenv),
            app_server_url=_env("IMCODEX_APP_SERVER_URL", "", dotenv) or None,
            app_server_experimental_api_enabled=_env_bool("IMCODEX_APP_SERVER_EXPERIMENTAL_API", False, dotenv),
            core_mode=_env("IMCODEX_CORE_MODE", "spawned-stdio", dotenv),
            core_url=_env("IMCODEX_CORE_URL", _env("IMCODEX_APP_SERVER_URL", "", dotenv), dotenv) or None,
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
                Path(path)
                if (path := _env("IMCODEX_APP_SERVER_AUTH_TOKEN_FILE", "", dotenv).strip())
                else None
            ),
            app_server_connect_max_attempts=_env_int("IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS", 3, dotenv),
            app_server_request_max_attempts=_env_int("IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS", 3, dotenv),
            app_server_retry_initial_delay_s=_env_float("IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY", 0.25, dotenv),
            app_server_retry_max_delay_s=_env_float("IMCODEX_APP_SERVER_RETRY_MAX_DELAY", 2.0, dotenv),
            app_server_retry_jitter_fraction=_env_float("IMCODEX_APP_SERVER_RETRY_JITTER", 0.25, dotenv),
            app_server_connect_timeout_s=_env_float("IMCODEX_APP_SERVER_CONNECT_TIMEOUT", 3.0, dotenv),
            app_server_health_timeout_s=_env_float("IMCODEX_APP_SERVER_HEALTH_TIMEOUT", 1.0, dotenv),
        )

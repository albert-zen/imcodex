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


@dataclass(slots=True)
class Settings:
    data_dir: Path
    codex_bin: str
    http_host: str
    http_port: int
    app_server_host: str
    app_server_port: int
    outbound_url: str | None
    service_name: str
    auto_approve: bool
    auto_approve_mode: str
    qq_enabled: bool
    qq_app_id: str
    qq_client_secret: str
    qq_api_base: str

    @property
    def app_server_ws_url(self) -> str:
        return f"ws://{self.app_server_host}:{self.app_server_port}"

    @classmethod
    def from_env(cls) -> "Settings":
        dotenv = _read_dotenv(Path(".env"))
        root = Path(_env("IMCODEX_DATA_DIR", ".imcodex", dotenv))
        return cls(
            data_dir=root,
            codex_bin=_env("IMCODEX_CODEX_BIN", "codex", dotenv),
            http_host=_env("IMCODEX_HTTP_HOST", "0.0.0.0", dotenv),
            http_port=int(_env("IMCODEX_HTTP_PORT", "8000", dotenv)),
            app_server_host=_env("IMCODEX_APP_SERVER_HOST", "127.0.0.1", dotenv),
            app_server_port=int(_env("IMCODEX_APP_SERVER_PORT", "8765", dotenv)),
            outbound_url=_env("IMCODEX_OUTBOUND_URL", "", dotenv) or None,
            service_name=_env("IMCODEX_SERVICE_NAME", "imcodex", dotenv),
            auto_approve=_env_bool("IMCODEX_AUTO_APPROVE", False, dotenv),
            auto_approve_mode=_env("IMCODEX_AUTO_APPROVE_MODE", "session", dotenv),
            qq_enabled=_env_bool("IMCODEX_QQ_ENABLED", False, dotenv),
            qq_app_id=_env("IMCODEX_QQ_APP_ID", "", dotenv),
            qq_client_secret=_env("IMCODEX_QQ_CLIENT_SECRET", "", dotenv),
            qq_api_base=_env("IMCODEX_QQ_API_BASE", "https://api.sgroup.qq.com", dotenv),
        )

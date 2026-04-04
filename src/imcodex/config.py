from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    data_dir: Path
    codex_bin: str
    app_server_host: str
    app_server_port: int
    outbound_url: str | None
    service_name: str
    qq_enabled: bool
    qq_app_id: str
    qq_client_secret: str
    qq_api_base: str

    @property
    def app_server_ws_url(self) -> str:
        return f"ws://{self.app_server_host}:{self.app_server_port}"

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.getenv("IMCODEX_DATA_DIR", ".imcodex"))
        return cls(
            data_dir=root,
            codex_bin=os.getenv("IMCODEX_CODEX_BIN", "codex"),
            app_server_host=os.getenv("IMCODEX_APP_SERVER_HOST", "127.0.0.1"),
            app_server_port=int(os.getenv("IMCODEX_APP_SERVER_PORT", "8765")),
            outbound_url=os.getenv("IMCODEX_OUTBOUND_URL") or None,
            service_name=os.getenv("IMCODEX_SERVICE_NAME", "imcodex"),
            qq_enabled=_env_bool("IMCODEX_QQ_ENABLED", False),
            qq_app_id=os.getenv("IMCODEX_QQ_APP_ID", ""),
            qq_client_secret=os.getenv("IMCODEX_QQ_CLIENT_SECRET", ""),
            qq_api_base=os.getenv("IMCODEX_QQ_API_BASE", "https://api.sgroup.qq.com"),
        )

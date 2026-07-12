from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


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
        if not user_id or not token:
            return
        self.context_tokens.pop(user_id, None)
        self.context_tokens[user_id] = token
        overflow = len(self.context_tokens) - max(1, limit)
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
        payload = self._read_json(self.credentials_path, required=True)
        account_id = str(payload.get("account_id") or "").strip()
        bot_token = str(payload.get("bot_token") or "").strip()
        base_url = str(payload.get("base_url") or "").strip()
        if not account_id or not bot_token or not base_url:
            raise RuntimeError(
                f"Invalid Weixin credential file: {self.credentials_path}. Run the login command again."
            )
        return WeixinCredentials(
            account_id=account_id,
            bot_token=bot_token,
            base_url=base_url,
            owner_user_id=str(payload.get("owner_user_id") or "").strip(),
            saved_at=str(payload.get("saved_at") or "").strip(),
        )

    def save_credentials(self, credentials: WeixinCredentials) -> None:
        payload = asdict(credentials)
        if not payload["saved_at"]:
            payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        payload["version"] = 1
        self._atomic_write_json(self.credentials_path, payload)

    def load_transport_state(self) -> WeixinTransportState:
        if not self.transport_state_path.exists():
            return WeixinTransportState()
        try:
            payload = self._read_json(self.transport_state_path, required=False)
        except (OSError, ValueError, json.JSONDecodeError):
            return WeixinTransportState()
        tokens = payload.get("context_tokens")
        context_tokens = (
            {
                str(user_id): str(token)
                for user_id, token in tokens.items()
                if str(user_id).strip() and str(token).strip()
            }
            if isinstance(tokens, dict)
            else {}
        )
        return WeixinTransportState(
            account_id=str(payload.get("account_id") or ""),
            get_updates_buf=str(payload.get("get_updates_buf") or ""),
            context_tokens=context_tokens,
        )

    def save_transport_state(self, state: WeixinTransportState) -> None:
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
        for path in (self.credentials_path, self.transport_state_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def clear_transport_state(self) -> None:
        try:
            self.transport_state_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if required:
                raise RuntimeError(f"Could not read Weixin state file: {path}") from None
            raise
        if not isinstance(payload, dict):
            if required:
                raise RuntimeError(f"Invalid Weixin state file: {path}")
            raise ValueError("state payload must be an object")
        return payload

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod(self.root, 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._chmod(temporary, 0o600)
        os.replace(temporary, path)
        self._chmod(path, 0o600)

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

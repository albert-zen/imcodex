from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


AccessMatch = Literal["any", "all"]


def parse_id_set(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    values: Iterable[object]
    if isinstance(value, str):
        values = value.replace("\n", ",").split(",")
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)
    return frozenset(text for item in values if (text := str(item).strip()))


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicy:
    """Product admission configuration passed to SDK Channel adapters."""

    allowed_user_ids: frozenset[str] = frozenset()
    allowed_conversation_ids: frozenset[str] = frozenset()
    access_match: AccessMatch = "any"

    def __post_init__(self) -> None:
        if self.access_match not in {"any", "all"}:
            raise ValueError("access_match must be 'any' or 'all'")
        configured = self.allowed_user_ids | self.allowed_conversation_ids
        if "none" in configured and configured != {"none"}:
            raise ValueError("'none' cannot be combined with any other access value")

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "ChannelAccessPolicy":
        return cls(
            allowed_user_ids=parse_id_set(config.get("allowed_user_ids")),
            allowed_conversation_ids=parse_id_set(config.get("allowed_conversation_ids")),
            access_match=str(config.get("access_match") or "any").strip().lower(),
        )

    @property
    def denies_all(self) -> bool:
        return "none" in self.allowed_user_ids or "none" in self.allowed_conversation_ids


def read_private_token_file(path: Path, *, label: str) -> str:
    try:
        if os.name != "nt":
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError(
                    f"{label} token file must be a private file (0600) and not a symlink: {path}"
                )
        value = path.read_text(encoding="utf-8").strip()
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Could not read {label} token file: {path}") from exc
    if not value:
        raise RuntimeError(f"{label} token file is empty: {path}")
    return value


def normalize_feishu_domain(value: str) -> str:
    normalized = value.strip().lower().rstrip("/")
    if normalized in {"feishu", "feishu.cn"}:
        return "feishu.cn"
    if normalized in {"lark", "larksuite", "larkoffice.com"}:
        return "larkoffice.com"
    raise ValueError("Feishu domain must be 'feishu' or 'lark'.")

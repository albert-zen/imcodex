from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


def parse_id_set(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        values: Iterable[object] = value.replace("\n", ",").split(",")
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)
    return frozenset(text for item in values if (text := str(item).strip()))


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicy:
    """Channel-owned gate based only on stable platform identifiers.

    An empty user allowlist denies every inbound message. Operators must list
    stable platform user IDs or explicitly opt into ``*``.
    """

    allowed_user_ids: frozenset[str]
    allowed_conversation_ids: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "ChannelAccessPolicy":
        return cls(
            allowed_user_ids=parse_id_set(config.get("allowed_user_ids")),
            allowed_conversation_ids=parse_id_set(config.get("allowed_conversation_ids")),
        )

    @classmethod
    def allow_all(cls) -> "ChannelAccessPolicy":
        return cls(allowed_user_ids=frozenset({"*"}))

    @property
    def has_allowed_users(self) -> bool:
        return bool(self.allowed_user_ids)

    def allows(self, *, user_id: str, conversation_id: str) -> bool:
        user_allowed = "*" in self.allowed_user_ids or user_id in self.allowed_user_ids
        if not user_allowed:
            return False
        if not self.allowed_conversation_ids or "*" in self.allowed_conversation_ids:
            return True
        return conversation_id in self.allowed_conversation_ids

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


AccessMatch = Literal["any", "all"]
_UNRESTRICTED = "*"
_DENY_ALL = "none"


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
    """Optional channel restrictions based on stable platform identifiers.

    Empty dimensions and ``*`` do not restrict platform-delivered messages.
    ``none`` explicitly denies the whole channel without disconnecting it.
    """

    allowed_user_ids: frozenset[str] = frozenset()
    allowed_conversation_ids: frozenset[str] = frozenset()
    access_match: AccessMatch = "any"

    def __post_init__(self) -> None:
        if self.access_match not in {"any", "all"}:
            raise ValueError("access_match must be 'any' or 'all'")
        configured_ids = self.allowed_user_ids | self.allowed_conversation_ids
        if _DENY_ALL in configured_ids and configured_ids != {_DENY_ALL}:
            raise ValueError("'none' cannot be combined with any other access value")

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "ChannelAccessPolicy":
        return cls(
            allowed_user_ids=parse_id_set(config.get("allowed_user_ids")),
            allowed_conversation_ids=parse_id_set(config.get("allowed_conversation_ids")),
            access_match=str(config.get("access_match") or "any").strip().lower(),
        )

    @classmethod
    def allow_all(cls) -> "ChannelAccessPolicy":
        return cls()

    @property
    def denies_all(self) -> bool:
        return _DENY_ALL in self.allowed_user_ids or _DENY_ALL in self.allowed_conversation_ids

    @property
    def restricted_user_ids(self) -> frozenset[str]:
        if _UNRESTRICTED in self.allowed_user_ids or self.denies_all:
            return frozenset()
        return self.allowed_user_ids

    @property
    def restricted_conversation_ids(self) -> frozenset[str]:
        if _UNRESTRICTED in self.allowed_conversation_ids or self.denies_all:
            return frozenset()
        return self.allowed_conversation_ids

    @property
    def active_restriction_count(self) -> int:
        return int(bool(self.restricted_user_ids)) + int(bool(self.restricted_conversation_ids))

    @property
    def mode(self) -> str:
        if self.denies_all:
            return "deny_all"
        if not self.active_restriction_count:
            return "platform"
        return f"restricted_{self.access_match}"

    def allows(self, *, user_id: str, conversation_id: str) -> bool:
        if self.denies_all:
            return False
        matches: list[bool] = []
        if self.restricted_user_ids:
            matches.append(user_id in self.restricted_user_ids)
        if self.restricted_conversation_ids:
            matches.append(conversation_id in self.restricted_conversation_ids)
        if not matches:
            return True
        return any(matches) if self.access_match == "any" else all(matches)

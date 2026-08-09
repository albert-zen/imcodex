from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .client import AppServerError


@dataclass(frozen=True, slots=True)
class NativeThreadAttachment:
    native_thread_id: str
    project_id: str
    provider_instance_id: str
    observer_thread_id: str
    disposition: str
    status: str


class NativeThreadObserverError(AppServerError):
    """A bounded, safe observer failure suitable for an IM-facing diagnosis."""

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        normalized_code = str(code or "observer_unavailable")
        self.http_status = http_status
        super().__init__(normalized_code)
        self.code = normalized_code


@runtime_checkable
class NativeThreadObserver(Protocol):
    async def ensure_ready(
        self,
        *,
        native_thread_id: str,
        cwd: str | None,
    ) -> NativeThreadAttachment: ...

    async def close(self) -> None: ...

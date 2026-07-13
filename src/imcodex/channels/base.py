from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from threading import Lock
import time

from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event, mark_channel_health
from .access import ChannelAccessPolicy


logger = logging.getLogger(__name__)
ACCESS_DENIAL_REPORT_LIMIT = 10
ACCESS_DENIAL_REPORT_WINDOW_S = 60.0


@dataclass(frozen=True, slots=True)
class ChannelRouteContext:
    admitted_user_id: str = ""
    last_inbound_message_id: str = ""


class _AccessDenialLimiter:
    """Bound access-denial diagnostics without weakening the actual gate."""

    def __init__(self) -> None:
        self._reported_at: deque[float] = deque()
        self._suppressed = 0
        self._lock = Lock()

    def note(self) -> int | None:
        now = time.monotonic()
        with self._lock:
            cutoff = now - ACCESS_DENIAL_REPORT_WINDOW_S
            while self._reported_at and self._reported_at[0] <= cutoff:
                self._reported_at.popleft()
            if len(self._reported_at) >= ACCESS_DENIAL_REPORT_LIMIT:
                self._suppressed += 1
                return None
            self._reported_at.append(now)
            suppressed = self._suppressed
            self._suppressed = 0
            return suppressed


class BaseChannelAdapter(ABC):
    channel_id: str

    def __init__(
        self,
        *,
        middleware,
        access_policy: ChannelAccessPolicy | None = None,
    ) -> None:
        self.middleware = middleware
        self.access_policy = access_policy or ChannelAccessPolicy(allowed_user_ids=frozenset())
        self._access_denial_limiter = _AccessDenialLimiter()

    def inbound_allowed(self, inbound: InboundMessage) -> bool:
        return self.access_policy.allows(
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
        )

    @property
    def inbound_access_ready(self) -> bool:
        return self.access_policy.has_allowed_users

    def access_policy_health(self) -> dict[str, object]:
        if not self.access_policy.has_allowed_users:
            mode = "deny_all"
        elif "*" in self.access_policy.allowed_user_ids and (
            not self.access_policy.allowed_conversation_ids
            or "*" in self.access_policy.allowed_conversation_ids
        ):
            mode = "open"
        else:
            mode = "restricted"
        return {
            "inbound_access_ready": self.inbound_access_ready,
            "access_policy_mode": mode,
            "allowed_user_count": len(self.access_policy.allowed_user_ids),
            "allowed_conversation_count": len(self.access_policy.allowed_conversation_ids),
        }

    def prepare_access_denial_report(self) -> int | None:
        return self._access_denial_limiter.note()

    def emit_access_denial(self, inbound: InboundMessage, suppressed: int) -> None:
        denial_reason = (
            "no_allowed_users"
            if not self.access_policy.has_allowed_users
            else "user_or_conversation_not_allowed"
        )
        logger.warning(
            "%s inbound message blocked by access policy user_id=%s conversation_id=%s suppressed_since_last=%d",
            self.channel_id,
            inbound.user_id,
            inbound.conversation_id,
            suppressed,
        )
        emit_event(
            component=f"channels.{self.channel_id}",
            event="message.inbound.access_denied",
            level="WARNING",
            message="Inbound channel message blocked by access policy",
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            user_id=inbound.user_id,
            message_id=inbound.message_id,
            data={"suppressed_since_last": suppressed},
        )
        mark_channel_health(
            self.channel_id,
            **self.access_policy_health(),
            last_inbound_access_denied_at=datetime.now(timezone.utc).isoformat(),
            last_inbound_access_denial_reason=denial_reason,
        )

    def ensure_outbound_allowed(self, message: OutboundMessage) -> None:
        user_id = str(self._last_inbound_user_id(message) or self._conversation_user_id(message.conversation_id) or "")
        if not user_id and "*" in self.access_policy.allowed_user_ids:
            user_id = "*"
        if user_id and self.access_policy.allows(
            user_id=user_id,
            conversation_id=message.conversation_id,
        ):
            return
        emit_event(
            component=f"channels.{self.channel_id}",
            event="message.outbound.access_denied",
            level="ERROR",
            message="Outbound channel message blocked by current access policy",
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            user_id=user_id or None,
        )
        raise PermissionError(f"{self.channel_id} outbound route is not admitted by the current access policy")

    def _last_inbound_user_id(self, message: OutboundMessage) -> str | None:
        context = self._route_context(message)
        user_id = context.admitted_user_id.strip() if context is not None else ""
        return user_id or None

    def _last_inbound_message_id(self, message: OutboundMessage) -> str | None:
        context = self._route_context(message)
        message_id = context.last_inbound_message_id.strip() if context is not None else ""
        return message_id or None

    def _route_context(self, message: OutboundMessage) -> ChannelRouteContext | None:
        resolver = getattr(self.middleware, "get_route_context", None)
        if not callable(resolver):
            return None
        context = resolver(message.channel_id, message.conversation_id)
        if not isinstance(context, ChannelRouteContext):
            return None
        return context

    def _conversation_user_id(self, conversation_id: str) -> str | None:
        return None

    async def dispatch_inbound(
        self,
        inbound: InboundMessage,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        if not self.inbound_allowed(inbound):
            suppressed = self.prepare_access_denial_report()
            if suppressed is not None:
                self.emit_access_denial(inbound, suppressed)
            return
        await self.middleware.handle_inbound(
            self,
            inbound,
            reply_to_message_id=reply_to_message_id,
        )

    @classmethod
    @abstractmethod
    def from_config(cls, *, config: dict[str, object], middleware):
        raise NotImplementedError

    def validate_startup_configuration(self) -> None:
        """Validate local prerequisites without opening a transport."""

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> None:
        raise NotImplementedError

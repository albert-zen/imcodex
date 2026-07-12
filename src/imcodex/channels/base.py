from __future__ import annotations

from abc import ABC, abstractmethod
import logging

from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event
from .access import ChannelAccessPolicy


logger = logging.getLogger(__name__)


class BaseChannelAdapter(ABC):
    channel_id: str

    def __init__(
        self,
        *,
        middleware,
        access_policy: ChannelAccessPolicy | None = None,
    ) -> None:
        self.middleware = middleware
        self.access_policy = access_policy or ChannelAccessPolicy.allow_all()

    async def dispatch_inbound(
        self,
        inbound: InboundMessage,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        if not self.access_policy.allows(
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
        ):
            logger.warning(
                "%s inbound message blocked by access policy user_id=%s conversation_id=%s",
                self.channel_id,
                inbound.user_id,
                inbound.conversation_id,
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
            )
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

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> None:
        raise NotImplementedError

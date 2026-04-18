from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import InboundMessage, OutboundMessage


class BaseChannelAdapter(ABC):
    channel_id: str

    def __init__(self, *, middleware) -> None:
        self.middleware = middleware

    async def dispatch_inbound(
        self,
        inbound: InboundMessage,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
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

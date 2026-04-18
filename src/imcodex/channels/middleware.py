from __future__ import annotations

import logging

from ..models import InboundMessage, OutboundMessage


logger = logging.getLogger(__name__)
GENERIC_USER_ERROR_TEXT = "Request failed while talking to Codex. Please try again."


class UnifiedChannelMiddleware:
    def __init__(self, *, service) -> None:
        self.service = service

    async def handle_inbound(
        self,
        adapter,
        inbound: InboundMessage,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        self._note_inbound_message(inbound)
        try:
            outbound = await self.service.handle_inbound(inbound)
            for message in outbound:
                if message.channel_id != adapter.channel_id:
                    continue
                if reply_to_message_id:
                    message.metadata.setdefault("reply_to_message_id", reply_to_message_id)
                await adapter.send_message(message)
        except Exception:
            logger.exception("%s inbound handling failed", adapter.channel_id)
            metadata = {}
            if reply_to_message_id:
                metadata["reply_to_message_id"] = reply_to_message_id
            await adapter.send_message(
                OutboundMessage(
                    channel_id=adapter.channel_id,
                    conversation_id=inbound.conversation_id,
                    message_type="error",
                    text=GENERIC_USER_ERROR_TEXT,
                    metadata=metadata,
                )
            )

    def _note_inbound_message(self, inbound: InboundMessage) -> None:
        store = getattr(self.service, "store", None)
        if store is None:
            return
        note = getattr(store, "note_inbound_message", None)
        if callable(note):
            note(inbound.channel_id, inbound.conversation_id, inbound.message_id)

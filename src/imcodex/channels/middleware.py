from __future__ import annotations

import logging

from ..models import InboundMessage, OutboundMessage
from ..observability.message_trace import ensure_trace_id, text_preview, text_sha256
from ..observability.runtime import emit_event


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
        trace_id = ensure_trace_id(inbound)
        emit_event(
            component=f"channels.{adapter.channel_id}",
            event="message.inbound.received",
            message="Inbound channel message received",
            trace_id=trace_id,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            user_id=inbound.user_id,
            message_id=inbound.message_id,
            data={
                "reply_to_message_id": reply_to_message_id,
                "text_length": len(inbound.text),
                "text_preview": text_preview(inbound.text),
                "text_sha256": text_sha256(inbound.text),
            },
        )
        self._note_inbound_message(inbound)
        try:
            outbound = await self.service.handle_inbound(inbound)
            for index, message in enumerate(outbound):
                if message.channel_id != adapter.channel_id:
                    continue
                message.metadata.setdefault("trace_id", trace_id)
                if reply_to_message_id:
                    message.metadata.setdefault("reply_to_message_id", reply_to_message_id)
                self._emit_outbound_event(
                    adapter=adapter,
                    message=message,
                    event="message.outbound.sending",
                    emitted_at="before_send",
                    outbound_index=index,
                )
                await adapter.send_message(message)
                self._emit_outbound_event(
                    adapter=adapter,
                    message=message,
                    event="message.outbound.sent",
                    emitted_at="after_send",
                    outbound_index=index,
                )
        except Exception:
            logger.exception("%s inbound handling failed", adapter.channel_id)
            metadata = {}
            if reply_to_message_id:
                metadata["reply_to_message_id"] = reply_to_message_id
            metadata["trace_id"] = trace_id
            error_message = OutboundMessage(
                channel_id=adapter.channel_id,
                conversation_id=inbound.conversation_id,
                message_type="error",
                text=GENERIC_USER_ERROR_TEXT,
                metadata=metadata,
            )
            self._emit_outbound_event(
                adapter=adapter,
                message=error_message,
                event="message.outbound.sending",
                emitted_at="before_send",
                outbound_index=0,
            )
            await adapter.send_message(
                error_message
            )
            self._emit_outbound_event(
                adapter=adapter,
                message=error_message,
                event="message.outbound.sent",
                emitted_at="after_send",
                outbound_index=0,
            )

    def _note_inbound_message(self, inbound: InboundMessage) -> None:
        store = getattr(self.service, "store", None)
        if store is None:
            return
        note = getattr(store, "note_inbound_message", None)
        if callable(note):
            note(inbound.channel_id, inbound.conversation_id, inbound.message_id)

    def _emit_outbound_event(
        self,
        *,
        adapter,
        message: OutboundMessage,
        event: str,
        emitted_at: str,
        outbound_index: int,
    ) -> None:
        trace_id = message.metadata.get("trace_id")
        emit_event(
            component=f"channels.{adapter.channel_id}",
            event=event,
            message="Outbound channel message emitted",
            trace_id=trace_id,
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            request_id=message.request_id,
            data={
                "emitted_at": emitted_at,
                "message_type": message.message_type,
                "outbound_index": outbound_index,
                "reply_to_message_id": message.metadata.get("reply_to_message_id"),
                "text_length": len(message.text),
                "text_preview": text_preview(message.text),
                "text_sha256": text_sha256(message.text),
            },
        )

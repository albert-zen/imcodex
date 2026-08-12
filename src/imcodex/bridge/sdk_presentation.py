from __future__ import annotations

from imagent.gateway.presentation import OutboundPresentationContext
from imagent.interaction.messages import OutboundMessage

from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
)


class ImcodexOutboundPresentation:
    """Apply IMCodex visibility preferences to already-routed SDK output."""

    def __init__(self, *, product_state) -> None:
        self.product_state = product_state

    async def present(
        self,
        message: OutboundMessage,
        context: OutboundPresentationContext,
    ) -> OutboundMessage | None:
        del context
        channel_id, conversation_id = self._product_route(message)
        state = self.product_state.get(channel_id, conversation_id)
        metadata = dict(message.metadata)
        phase = str(metadata.get("phase") or "").casefold()
        kind = str(metadata.get("kind") or "").casefold()
        native_method = str(metadata.get("native_method") or "").casefold()

        # Final answers, request responses, and product command output are
        # always visible.  Only optional live/commentary/tool/system output is
        # governed by the IM presentation preference.
        if phase in {"final", "final_answer", "finalanswer"} or kind in {
            "artifact_materialization",
            "artifact_terminal_fallback",
        }:
            return message
        if metadata.get("imcodex_product_controller") or metadata.get("request_kind"):
            return message
        if metadata.get("live_only") is True:
            visible = bool(state.get("show_system", False))
        elif phase == "commentary" or native_method in {
            "item/agentmessage/delta",
            "item/completed",
        }:
            visible = bool(state.get("show_commentary", True))
        elif kind in {"command_execution", "file_change", "diff_updated", "plan_updated"}:
            visible = bool(state.get("show_toolcalls", False))
        else:
            visible = bool(state.get("show_system", False))
        return message if visible else None

    @staticmethod
    def _product_route(message: OutboundMessage) -> tuple[str, str]:
        conversation = message.conversation_ref
        if conversation.channel_instance_id == WEBHOOK_CHANNEL_INSTANCE_ID:
            return decode_webhook_conversation(conversation.native_conversation_id)
        return conversation.channel_instance_id, conversation.native_conversation_id

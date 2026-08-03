from __future__ import annotations

from datetime import UTC, datetime

from imagent.adapters import DeliverySubmissionConflict
from imagent.contracts import (
    AttachmentContent,
    ConversationDeliveryTarget,
    ConversationRef,
    DeliveryIntent,
    DeliveryItemStatus,
    DeliveryPrincipal,
    DeliveryReceiptStatus,
    DeliverySubmissionState,
    LocalPath,
    TextContent,
)
from imagent.delivery_outcomes import DeliveryOutcome, DeliveryOutcomeContext
from imagent.proactive_delivery import ScopedDeliveryAuthorizer

from ..models import OutboundMessage


class ImcodexDeliveryOutcomeObserver:
    """Release product-owned spool leases after one complete SDK delivery."""

    def __init__(self, *, artifact_ledger) -> None:
        self.artifact_ledger = artifact_ledger

    async def observe_delivery_outcome(
        self,
        context: DeliveryOutcomeContext,
        outcome: DeliveryOutcome,
    ) -> None:
        receipt = outcome.receipt
        if (
            receipt is not None
            and receipt.status is DeliveryReceiptStatus.RETRYABLE_FAILURE
        ):
            return
        attachments = {
            index: item.source.path
            for index, item in enumerate(context.message.content)
            if isinstance(item, AttachmentContent)
            and isinstance(item.source, LocalPath)
        }
        paths = tuple(attachments.values())
        if receipt is not None and receipt.items:
            terminal_statuses = {
                DeliveryItemStatus.ACCEPTED,
                DeliveryItemStatus.REJECTED,
            }
            paths = tuple(
                attachments[item.content_index]
                for item in receipt.items
                if item.content_index in attachments
                and item.status in terminal_statuses
            )
        self.artifact_ledger.complete_attempt(context.message.delivery_id, paths)


class ImcodexProactiveDelivery:
    """Current product delivery API translated onto SDK proactive delivery."""

    def __init__(
        self,
        *,
        gateway,
        authorizer: ScopedDeliveryAuthorizer,
        product_store,
        artifact_ledger,
        registered_channel_ids: set[str],
        fallback_excluded_channel_ids: set[str],
        webhook_channel,
    ) -> None:
        self.gateway = gateway
        self.authorizer = authorizer
        self.product_store = product_store
        self.artifact_ledger = artifact_ledger
        self.registered_channel_ids = registered_channel_ids
        self.fallback_excluded_channel_ids = fallback_excluded_channel_ids
        self.webhook_channel = webhook_channel

    def can_deliver_outbound(self, channel_id: str) -> bool:
        if channel_id in self.registered_channel_ids:
            return True
        return (
            channel_id not in self.fallback_excluded_channel_ids
            and self.webhook_channel.outbound_sink is not None
        )

    def resolve_outbound_route(self, source_thread_id: str) -> tuple[str, str]:
        thread_id = str(source_thread_id or "").strip()
        route = (
            self.product_store.find_recipient_route_by_thread_id(thread_id)
            if thread_id
            else None
        )
        if route is None:
            raise ValueError(
                "This Codex thread has not been selected from an IM conversation. "
                "Open or pick it from IMCodex once, then retry."
            )
        return route

    def validate_outbound_message(self, message: OutboundMessage) -> None:
        if not self.can_deliver_outbound(message.channel_id):
            raise ValueError(
                f"Configured channel {message.channel_id!r} is unavailable."
            )

    async def stage_outbound_upload(self, content: bytes, **kwargs):
        return await self.artifact_ledger.stage_upload(content, **kwargs)

    async def discard_outbound_uploads(self, artifacts) -> None:
        self.artifact_ledger.discard_request(artifacts)

    async def deliver_outbound_message(
        self,
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        conversation = self._conversation_ref(
            message.channel_id,
            message.conversation_id,
        )
        content = []
        if message.text:
            content.append(TextContent(message.text))
        for index, artifact in enumerate(message.artifacts):
            content.append(
                AttachmentContent(
                    attachment_id=f"artifact-{index}",
                    media_type=artifact.content_type,
                    source=LocalPath(artifact.local_path),
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                    metadata={"sha256": artifact.sha256, "kind": artifact.kind},
                )
            )
        intent = DeliveryIntent(
            delivery_id=str(message.metadata.get("delivery_id") or ""),
            target=ConversationDeliveryTarget(conversation),
            content=tuple(content),
            created_at=datetime.now(UTC),
            metadata={"imcodex_message_type": message.message_type},
        )
        credential = await self.authorizer.issue(
            DeliveryPrincipal(
                principal_id="imcodex:local-delivery",
                allowed_conversations=(conversation,),
            )
        )
        self.artifact_ledger.transfer(intent.delivery_id, message.artifacts)
        try:
            result = await self.gateway.deliver_proactively(
                intent,
                credential=credential,
            )
        except DeliverySubmissionConflict as exc:
            self.artifact_ledger.complete_delivery(
                intent.delivery_id,
                tuple(artifact.local_path for artifact in message.artifacts),
            )
            raise ValueError(str(exc)) from None
        finally:
            await self.authorizer.revoke(credential)

        self._apply_item_receipts(message, result, text_offset=bool(message.text))
        if result.state is DeliverySubmissionState.ACCEPTED:
            return [message], True, True
        if result.state is DeliverySubmissionState.PARTIAL:
            return [message], False, False
        if result.state is DeliverySubmissionState.RETRYABLE:
            return [message], False, True
        if result.state is DeliverySubmissionState.REJECTED:
            raise ValueError(result.error or "delivery was rejected")
        return [message], False, False

    def _conversation_ref(
        self, channel_id: str, conversation_id: str
    ) -> ConversationRef:
        if channel_id in self.registered_channel_ids:
            return ConversationRef(channel_id, conversation_id)
        return self.webhook_channel.conversation_ref_for(channel_id, conversation_id)

    @staticmethod
    def _apply_item_receipts(message, result, *, text_offset: bool) -> None:
        recorded = []
        for destination in result.destinations:
            receipt = destination.receipt
            if receipt is None:
                continue
            for item in receipt.items:
                artifact_index = item.content_index - int(text_offset)
                if not 0 <= artifact_index < len(message.artifacts):
                    continue
                artifact = message.artifacts[artifact_index]
                recorded.append(
                    {
                        "local_path": artifact.local_path,
                        "sha256": artifact.sha256,
                        "filename": artifact.filename,
                        "status": (
                            "delivered"
                            if item.status is DeliveryItemStatus.ACCEPTED
                            else "failed"
                        ),
                        "error": item.detail or "",
                        "platform_message_id": item.native_message_id or "",
                    }
                )
        if recorded:
            message.metadata["artifact_receipts"] = recorded

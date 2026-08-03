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
    DeliverySegmentStatus,
    DeliverySubmissionState,
    LocalPath,
    TextContent,
    ThreadRef,
    ThreadRouteDeliveryTarget,
)
from imagent.delivery_outcomes import DeliveryOutcome, DeliveryOutcomeContext
from imagent.proactive_delivery import DeliveryRouteError, ScopedDeliveryAuthorizer

from ..models import OutboundMessage
from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
)


class ImcodexDeliveryOutcomeObserver:
    """Release product-owned spool leases after one complete SDK delivery."""

    def __init__(self, *, artifact_ledger) -> None:
        self.artifact_ledger = artifact_ledger

    async def observe_delivery_outcome(
        self,
        context: DeliveryOutcomeContext,
        outcome: DeliveryOutcome,
    ) -> None:
        paths = _terminal_artifact_paths(context.message.content, outcome.receipt)
        if paths:
            self.artifact_ledger.complete_attempt(context.message.delivery_id, paths)


class ImcodexProactiveDelivery:
    """Current product delivery API translated onto SDK proactive delivery."""

    def __init__(
        self,
        *,
        gateway,
        authorizer: ScopedDeliveryAuthorizer,
        artifact_ledger,
        application_instance_id: str,
        registered_channel_ids: set[str],
        fallback_excluded_channel_ids: set[str],
        webhook_channel,
    ) -> None:
        self.gateway = gateway
        self.authorizer = authorizer
        self.artifact_ledger = artifact_ledger
        self.application_instance_id = application_instance_id
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

    def validate_outbound_message(self, message: OutboundMessage) -> None:
        if str(message.metadata.get("source_thread_id") or "").strip():
            return
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
        source_thread_id = str(
            message.metadata.get("source_thread_id") or ""
        ).strip()
        thread_ref = (
            ThreadRef(self.application_instance_id, source_thread_id)
            if source_thread_id
            else None
        )
        conversation = (
            None
            if thread_ref is not None
            else self._conversation_ref(
                message.channel_id,
                message.conversation_id,
            )
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
            target=(
                ThreadRouteDeliveryTarget(thread_ref)
                if thread_ref is not None
                else ConversationDeliveryTarget(conversation)
            ),
            content=tuple(content),
            created_at=datetime.now(UTC),
            metadata={"imcodex_message_type": message.message_type},
        )
        credential = await self.authorizer.issue(
            DeliveryPrincipal(
                principal_id="imcodex:local-delivery",
                allowed_threads=(thread_ref,) if thread_ref is not None else (),
                allowed_conversations=(conversation,) if conversation is not None else (),
            )
        )
        created_lease = False
        try:
            created_lease = self.artifact_ledger.transfer(
                intent.delivery_id, message.artifacts
            )
            result = await self.gateway.deliver_proactively(
                intent,
                credential=credential,
            )
        except (DeliverySubmissionConflict, DeliveryRouteError) as exc:
            if created_lease:
                self.artifact_ledger.complete_delivery(
                    intent.delivery_id,
                    tuple(artifact.local_path for artifact in message.artifacts),
                )
            raise ValueError(str(exc)) from None
        finally:
            await self.authorizer.revoke(credential)

        outbound = self._destination_messages(message, result)
        self._reconcile_terminal_result(intent.delivery_id, result, intent.content)
        if result.state is DeliverySubmissionState.ACCEPTED:
            return outbound, True, True
        if result.state is DeliverySubmissionState.PARTIAL:
            return outbound, False, True
        if result.state is DeliverySubmissionState.RETRYABLE:
            return outbound, False, True
        if result.state is DeliverySubmissionState.REJECTED:
            raise ValueError(result.error or "delivery was rejected")
        return outbound, False, False

    def _destination_messages(self, message, result) -> list[OutboundMessage]:
        outbound: list[OutboundMessage] = []
        message.metadata["sdk_submission_state"] = result.state.value
        for destination in result.destinations:
            conversation = destination.conversation_ref
            if conversation is None:
                continue
            channel_id, conversation_id = self._product_route(conversation)
            projected = OutboundMessage(
                channel_id=channel_id,
                conversation_id=conversation_id,
                message_type=message.message_type,
                text=message.text,
                request_id=message.request_id,
                metadata={
                    **message.metadata,
                    "destination_delivery_id": destination.delivery_id,
                    "destination_state": destination.state.value,
                    "destination_error": destination.error or "",
                },
                artifacts=list(message.artifacts),
            )
            self._apply_item_receipts(
                projected,
                (destination,),
                text_offset=bool(message.text),
            )
            outbound.append(projected)
        if outbound:
            return outbound
        message.metadata["destination_count"] = len(result.destinations)
        message.metadata["destination_states"] = tuple(
            destination.state.value for destination in result.destinations
        )
        self._apply_item_receipts(
            message,
            result.destinations,
            text_offset=bool(message.text),
        )
        return [message]

    def _reconcile_terminal_result(self, delivery_id: str, result, content) -> None:
        """Converge leases when an SDK terminal replay does not emit O2 again."""

        if any(
            destination.state
            in {
                DeliverySubmissionState.IN_FLIGHT,
                DeliverySubmissionState.RETRYABLE,
            }
            for destination in result.destinations
        ):
            return
        paths = tuple(
            item.source.path
            for item in content
            if isinstance(item, AttachmentContent) and isinstance(item.source, LocalPath)
        )
        if paths:
            self.artifact_ledger.complete_delivery(delivery_id, paths)

    def _conversation_ref(
        self, channel_id: str, conversation_id: str
    ) -> ConversationRef:
        if channel_id in self.registered_channel_ids:
            return ConversationRef(channel_id, conversation_id)
        return self.webhook_channel.conversation_ref_for(channel_id, conversation_id)

    @staticmethod
    def _product_route(conversation: ConversationRef) -> tuple[str, str]:
        if conversation.channel_instance_id == WEBHOOK_CHANNEL_INSTANCE_ID:
            return decode_webhook_conversation(conversation.native_conversation_id)
        return (
            conversation.channel_instance_id,
            conversation.native_conversation_id,
        )

    @staticmethod
    def _apply_item_receipts(message, destinations, *, text_offset: bool) -> None:
        recorded = []
        for destination in destinations:
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


def _terminal_artifact_paths(
    content,
    receipt,
    *,
    submission_state: DeliverySubmissionState | None = None,
) -> tuple[str, ...]:
    if (
        receipt is not None
        and receipt.status is DeliveryReceiptStatus.RETRYABLE_FAILURE
    ):
        return ()
    attachments = {
        index: item.source.path
        for index, item in enumerate(content)
        if isinstance(item, AttachmentContent) and isinstance(item.source, LocalPath)
    }
    if receipt is not None and receipt.status is DeliveryReceiptStatus.UNKNOWN:
        return tuple(attachments.values())
    if (
        receipt is not None
        and receipt.status is DeliveryReceiptStatus.REJECTED_BY_PLATFORM
        and not any(
            item.status is DeliveryItemStatus.ACCEPTED for item in receipt.items
        )
        and not any(
            segment.status is DeliverySegmentStatus.ACCEPTED_BY_PLATFORM
            for segment in receipt.segments
        )
    ):
        return tuple(attachments.values())
    if submission_state in {
        DeliverySubmissionState.ACCEPTED,
        DeliverySubmissionState.REJECTED,
        DeliverySubmissionState.UNKNOWN,
    }:
        return tuple(attachments.values())
    if receipt is not None and receipt.items:
        terminal_statuses = {DeliveryItemStatus.ACCEPTED, DeliveryItemStatus.REJECTED}
        return tuple(
            attachments[item.content_index]
            for item in receipt.items
            if item.content_index in attachments and item.status in terminal_statuses
        )
    return tuple(attachments.values())

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from imagent.gateway.delivery import (
    ConversationDeliveryTarget,
    DeliveryIntent,
    DeliveryPrincipal,
    DeliverySubmissionState,
)
from imagent.interaction.channels import (
    ChannelCapabilities,
    DeliveryReceipt,
    DeliveryReceiptStatus,
    DeliverySupportLevel,
)
from imagent.interaction.media import AttachmentSourceKind, LocalPath
from imagent.interaction.messages import (
    AttachmentContent,
    ConversationRef,
    InboundMessage as SdkInboundMessage,
    OutboundMessage as SdkOutboundMessage,
    TextContent,
    TextFormat,
)

from ..delivery_artifacts import DeliveryArtifactStager
from ..models import InboundMessage, OutboundArtifact, OutboundMessage
from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
    encode_webhook_conversation,
)


class SdkWebhookChannel:
    """Product HTTP ingress adapted to the public SDK Channel port."""

    kind = "webhook"

    capabilities = ChannelCapabilities(
        markdown=DeliverySupportLevel.NATIVE,
        attachments=DeliverySupportLevel.NATIVE,
        attachment_sources=(AttachmentSourceKind.LOCAL_PATH,),
        reply_references=DeliverySupportLevel.NATIVE,
        max_text_length=32 * 1024,
    )

    def __init__(self, *, outbound_sink=None) -> None:
        self._started = False
        self._on_message = None
        self._on_admission = None
        self._pending: dict[str, list[OutboundMessage]] = {}
        self.outbound_sink = outbound_sink

    @property
    def channel_instance_id(self) -> str:
        return WEBHOOK_CHANNEL_INSTANCE_ID

    async def start(self, on_message, on_admission=None) -> None:
        if self._started:
            raise RuntimeError("webhook Channel is already started")
        self._on_message = on_message
        self._on_admission = on_admission
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self._on_message = None
        self._on_admission = None
        self._pending.clear()

    async def receive(self, inbound: InboundMessage) -> list[OutboundMessage]:
        """Submit one product webhook message through Gateway ingress."""

        if not self._started or self._on_message is None:
            raise RuntimeError("webhook Channel is not started")
        message = self._to_sdk_inbound(inbound)
        outputs: list[OutboundMessage] = []
        self._pending[message.message_id] = outputs
        try:
            if self._on_admission is None:
                await self._on_message(message)
            else:
                admission = await self._on_admission(
                    message.conversation_ref,
                    message.message_id,
                )
                if admission is not None:
                    await admission.deliver(message)
        finally:
            self._pending.pop(message.message_id, None)
        return outputs

    async def send(self, message: SdkOutboundMessage) -> DeliveryReceipt:
        channel_id, conversation_id = self._product_route(message.conversation_ref)
        product = self._to_product_outbound(message, channel_id, conversation_id)
        reply_to = message.reply_to
        immediate = self._pending.get(reply_to or "")
        if immediate is not None:
            if not any(item.metadata.get("delivery_id") == message.delivery_id for item in immediate):
                immediate.append(product)
            return DeliveryReceipt(
                status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM,
                native_message_id=message.delivery_id,
            )
        if self.outbound_sink is None:
            raise RuntimeError(
                "webhook output has no synchronous request and no outbound URL is configured"
            )
        await self.outbound_sink.send_message(product)
        return DeliveryReceipt(
            status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM,
            native_message_id=message.delivery_id,
        )

    def _conversation_ref(self, channel_id: str, conversation_id: str) -> ConversationRef:
        return ConversationRef(
            channel_instance_id=self.channel_instance_id,
            native_conversation_id=encode_webhook_conversation(channel_id, conversation_id),
        )

    @classmethod
    def _to_sdk_inbound(cls, message: InboundMessage) -> SdkInboundMessage:
        content: list[TextContent | AttachmentContent] = []
        if message.text:
            content.append(TextContent(message.text, TextFormat.PLAIN))
        for attachment in message.attachments:
            content.append(
                AttachmentContent(
                    attachment_id=(
                        f"imcodex:inbound:{attachment.source_message_id or message.message_id}:"
                        f"{len(content)}"
                    ),
                    media_type=attachment.content_type,
                    source=LocalPath(attachment.local_path),
                    filename=attachment.filename or None,
                    size_bytes=attachment.size_bytes,
                    metadata={
                        "kind": attachment.kind,
                        "source_channel_id": attachment.source_channel_id,
                        "source_message_id": attachment.source_message_id,
                    },
                )
            )
        created_at = cls._parse_timestamp(message.sent_at)
        return SdkInboundMessage(
            message_id=message.message_id,
            conversation_ref=cls._conversation_ref_static(
                message.channel_id,
                message.conversation_id,
            ),
            sender=message.user_id,
            content=tuple(content),
            created_at=created_at,
            reply_to=message.reply_to_message_id,
            metadata={
                "channel_id": message.channel_id,
                "native_conversation_id": message.conversation_id,
                "input_error": message.input_error,
                "trace_id": message.trace_id,
            },
        )

    @staticmethod
    def _conversation_ref_static(channel_id: str, conversation_id: str) -> ConversationRef:
        return ConversationRef(
            channel_instance_id=WEBHOOK_CHANNEL_INSTANCE_ID,
            native_conversation_id=encode_webhook_conversation(channel_id, conversation_id),
        )

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    @staticmethod
    def _product_route(conversation_ref: ConversationRef) -> tuple[str, str]:
        if conversation_ref.channel_instance_id != WEBHOOK_CHANNEL_INSTANCE_ID:
            return (
                conversation_ref.channel_instance_id,
                conversation_ref.native_conversation_id,
            )
        return decode_webhook_conversation(conversation_ref.native_conversation_id)

    @staticmethod
    def _to_product_outbound(
        message: SdkOutboundMessage,
        channel_id: str,
        conversation_id: str,
    ) -> OutboundMessage:
        text_parts: list[str] = []
        artifacts: list[OutboundArtifact] = []
        for item in message.content:
            if isinstance(item, TextContent):
                text_parts.append(item.text)
            elif isinstance(item, AttachmentContent) and isinstance(item.source, LocalPath):
                metadata = dict(item.metadata)
                artifacts.append(
                    OutboundArtifact(
                        kind=(
                            "image"
                            if str(metadata.get("kind") or "").casefold() == "image"
                            or item.media_type.startswith("image/")
                            else "file"
                        ),
                        local_path=item.source.path,
                        content_type=item.media_type,
                        filename=str(item.filename or "attachment"),
                        size_bytes=int(item.size_bytes or 0),
                        sha256=str(metadata.get("sha256") or ""),
                    )
                )
        metadata = dict(message.metadata)
        metadata["delivery_id"] = message.delivery_id
        if message.reply_to:
            metadata.setdefault("reply_to_message_id", message.reply_to)
        return OutboundMessage(
            channel_id=channel_id,
            conversation_id=conversation_id,
            message_type=str(metadata.get("imcodex_message_type") or "message"),
            text="\n".join(text_parts),
            request_id=(str(metadata.get("request_id") or "") or None),
            metadata=metadata,
            artifacts=artifacts,
        )


class SdkRuntimeService:
    """Thin product surface over the SDK Gateway and App Server client."""

    def __init__(
        self,
        *,
        channel: SdkWebhookChannel,
        gateway,
        client,
        product_state,
        channels=(),
        delivery_authorizer=None,
        artifact_stager: DeliveryArtifactStager | None = None,
    ) -> None:
        self.channel = channel
        self.gateway = gateway
        self.client = client
        self.product_state = product_state
        self.outbound_sink = channel.outbound_sink
        self._channels = tuple(channels)
        self._channel_ids = frozenset(
            str(getattr(item, "channel_instance_id", ""))
            for item in self._channels
            if getattr(item, "channel_instance_id", None)
        )
        self._delivery_authorizer = delivery_authorizer
        self._artifact_stager = artifact_stager

    async def handle_inbound(self, message: InboundMessage, **_options) -> list[OutboundMessage]:
        return await self.channel.receive(message)

    async def close(self) -> None:
        if self._artifact_stager is not None:
            await asyncio.to_thread(self._artifact_stager.cleanup_unreferenced, set())

    def can_deliver_outbound(self, channel_id: str) -> bool:
        return channel_id in self._channel_ids or self.outbound_sink is not None

    def validate_outbound_message(self, message: OutboundMessage) -> None:
        if not message.channel_id and not message.metadata.get("source_thread_id"):
            raise ValueError("a channel route or source thread is required")
        if message.metadata.get("source_thread_id"):
            raise ValueError(
                "current-thread delivery is unavailable through public SDK v1; "
                "provide an explicit channel and conversation"
            )
        if not self.can_deliver_outbound(message.channel_id):
            raise ValueError(f"Configured channel {message.channel_id!r} is unavailable.")

    def resolve_outbound_route(self, source_thread_id: str) -> tuple[str, str]:
        del source_thread_id
        raise ValueError(
            "current-thread delivery is unavailable through public SDK v1; "
            "the SDK exposes no public remembered-recipient lookup"
        )

    async def stage_outbound_upload(
        self,
        content: bytes,
        *,
        kind: str,
        content_type: str,
        filename: str,
    ) -> OutboundArtifact:
        if self._artifact_stager is None:
            raise ValueError("artifact staging is unavailable")
        return await asyncio.to_thread(
            self._artifact_stager.stage_upload,
            content,
            kind=kind,
            content_type=content_type,
            filename=filename,
        )

    async def discard_outbound_uploads(self, artifacts) -> None:
        if self._artifact_stager is not None:
            await asyncio.to_thread(self._artifact_stager.release, artifacts)

    async def deliver_outbound_message(
        self,
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        self.validate_outbound_message(message)
        if message.channel_id not in self._channel_ids and self.outbound_sink is not None:
            await self.outbound_sink.send_message(message)
            return [message], True, True
        conversation_ref = self._delivery_conversation_ref(
            message.channel_id,
            message.conversation_id,
        )
        if conversation_ref.channel_instance_id == WEBHOOK_CHANNEL_INSTANCE_ID and (
            self.outbound_sink is not None
        ):
            await self.outbound_sink.send_message(message)
            return [message], True, True
        if self._delivery_authorizer is None:
            raise RuntimeError("SDK proactive delivery authorization is unavailable")
        content: list[TextContent | AttachmentContent] = []
        if message.text:
            content.append(TextContent(message.text, TextFormat.MARKDOWN))
        for index, artifact in enumerate(message.artifacts):
            content.append(
                AttachmentContent(
                    attachment_id=f"imcodex:delivery:{message.metadata.get('delivery_id') or 'request'}:{index}",
                    media_type=artifact.content_type,
                    source=LocalPath(artifact.local_path),
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                    metadata={"kind": artifact.kind, "sha256": artifact.sha256},
                )
            )
        intent = DeliveryIntent(
            delivery_id=str(message.metadata.get("delivery_id") or "imcodex:delivery"),
            target=ConversationDeliveryTarget(conversation_ref),
            content=tuple(content),
            created_at=datetime.now(UTC),
            metadata={"imcodex_message_type": message.message_type},
        )
        credential = await self._delivery_authorizer.issue(
            DeliveryPrincipal(
                principal_id="imcodex:local-delivery",
                allowed_conversations=(conversation_ref,),
            )
        )
        try:
            result = await self.gateway.deliver_proactively(intent, credential=credential)
        finally:
            await self._delivery_authorizer.revoke(credential)
        message.metadata["sdk_submission_state"] = result.state.value
        if result.error:
            message.metadata["sdk_delivery_error"] = result.error
        if result.state is DeliverySubmissionState.ACCEPTED:
            return [message], True, True
        if result.state in {
            DeliverySubmissionState.IN_FLIGHT,
            DeliverySubmissionState.RETRYABLE,
            DeliverySubmissionState.PARTIAL,
        }:
            return [message], False, True
        if result.state is DeliverySubmissionState.UNKNOWN:
            return [message], False, False
        raise ValueError(result.error or "SDK proactive delivery was rejected")

    def state_for(self, channel_id: str, conversation_id: str) -> dict[str, Any]:
        return self.product_state.get(channel_id, conversation_id)

    def update_state(self, channel_id: str, conversation_id: str, **values: Any) -> dict[str, Any]:
        return self.product_state.update(channel_id, conversation_id, **values)

    @staticmethod
    def _delivery_conversation_ref(channel_id: str, conversation_id: str) -> ConversationRef:
        if channel_id == WEBHOOK_CHANNEL_INSTANCE_ID:
            return ConversationRef(
                channel_instance_id=WEBHOOK_CHANNEL_INSTANCE_ID,
                native_conversation_id=encode_webhook_conversation(
                    WEBHOOK_CHANNEL_INSTANCE_ID,
                    conversation_id,
                ),
            )
        return ConversationRef(channel_id, conversation_id)

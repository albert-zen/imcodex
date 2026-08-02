from __future__ import annotations

from datetime import UTC, datetime

from imagent.contracts import (
    AttachmentContent,
    AttachmentSourceKind,
    ChannelCapabilities,
    ConversationRef,
    DeliveryReceipt,
    DeliveryReceiptStatus,
    InboundMessage as SdkInboundMessage,
    LocalPath,
    OutboundMessage as SdkOutboundMessage,
    SupportLevel,
    TextContent,
    TextFormat,
)

from ..models import InboundMessage, OutboundArtifact, OutboundMessage
from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
    encode_webhook_conversation,
)
from .registry import BUILTIN_CHANNEL_IDS


class SdkWebhookChannel:
    """Product HTTP ingress as one multiplexed SDK Channel."""

    channel_instance_id = WEBHOOK_CHANNEL_INSTANCE_ID
    kind = "webhook"
    capabilities = ChannelCapabilities(
        markdown=SupportLevel.FALLBACK,
        attachments=SupportLevel.NATIVE,
        attachment_sources=(AttachmentSourceKind.LOCAL_PATH,),
        reply_references=SupportLevel.NATIVE,
        max_text_length=100_000,
    )

    def __init__(self, *, outbound_sink=None) -> None:
        self.outbound_sink = outbound_sink
        self._on_message = None
        self._on_operation = None
        self._on_admission = None
        self._pending: dict[tuple[ConversationRef, str], list[OutboundMessage]] = {}

    async def start(self, on_message, on_operation, on_admission=None) -> None:
        self._on_message = on_message
        self._on_operation = on_operation
        self._on_admission = on_admission

    async def stop(self) -> None:
        self._on_message = None
        self._on_operation = None
        self._on_admission = None
        self._pending.clear()

    async def receive(self, message: InboundMessage) -> list[OutboundMessage]:
        if self._on_message is None:
            raise RuntimeError("webhook Channel is not started")
        conversation = self._conversation_ref(message.channel_id, message.conversation_id)
        key = (conversation, message.message_id)
        responses: list[OutboundMessage] = []
        self._pending[key] = responses
        sdk_message = self._to_sdk_inbound(message, conversation)
        try:
            if self._on_admission is None:
                await self._on_message(sdk_message)
            else:
                admission = await self._on_admission(conversation, message.message_id)
                if admission is not None:
                    await admission.deliver(sdk_message)
            return responses
        finally:
            self._pending.pop(key, None)

    async def send(self, message: SdkOutboundMessage) -> DeliveryReceipt:
        channel_id, conversation_id = self._decode_conversation(message.conversation_ref)
        legacy = self._to_product_outbound(message, channel_id, conversation_id)
        pending = self._pending.get((message.conversation_ref, str(message.reply_to or "")))
        if pending is not None:
            pending.append(legacy)
        elif self.outbound_sink is not None and channel_id not in BUILTIN_CHANNEL_IDS:
            await self.outbound_sink.send_message(legacy)
        else:
            return DeliveryReceipt(
                status=DeliveryReceiptStatus.REJECTED,
                detail="No outbound webhook is configured for asynchronous delivery",
            )
        return DeliveryReceipt(status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM)

    @classmethod
    def _conversation_ref(cls, channel_id: str, conversation_id: str) -> ConversationRef:
        return ConversationRef(
            cls.channel_instance_id,
            encode_webhook_conversation(channel_id, conversation_id),
        )

    conversation_ref_for = _conversation_ref

    @staticmethod
    def _decode_conversation(conversation: ConversationRef) -> tuple[str, str]:
        return decode_webhook_conversation(conversation.native_conversation_id)

    @staticmethod
    def _to_sdk_inbound(
        message: InboundMessage,
        conversation: ConversationRef,
    ) -> SdkInboundMessage:
        content: list[TextContent | AttachmentContent] = []
        if message.text:
            content.append(TextContent(message.text, TextFormat.PLAIN))
        for index, artifact in enumerate(message.attachments):
            content.append(
                AttachmentContent(
                    attachment_id=(
                        artifact.source_message_id
                        or f"{message.message_id}:attachment:{index}"
                    ),
                    media_type=artifact.content_type,
                    source=LocalPath(artifact.local_path),
                    filename=artifact.filename or None,
                    size_bytes=artifact.size_bytes,
                    metadata={"kind": artifact.kind},
                )
            )
        try:
            created_at = datetime.fromisoformat(str(message.sent_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            created_at = datetime.now(UTC)
        return SdkInboundMessage(
            message_id=message.message_id,
            conversation_ref=conversation,
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
    def _to_product_outbound(message, channel_id, conversation_id) -> OutboundMessage:
        text = "\n".join(
            item.text for item in message.content if isinstance(item, TextContent)
        )
        artifacts = [
            OutboundArtifact(
                kind=(
                    "image"
                    if str(item.metadata.get("kind") or "").casefold() == "image"
                    or item.media_type.startswith("image/")
                    else "file"
                ),
                local_path=item.source.path,
                content_type=item.media_type,
                filename=str(item.filename or ""),
                size_bytes=int(item.size_bytes or 0),
                sha256=str(item.metadata.get("sha256") or ""),
            )
            for item in message.content
            if isinstance(item, AttachmentContent) and isinstance(item.source, LocalPath)
        ]
        metadata = dict(message.metadata)
        metadata["delivery_id"] = message.delivery_id
        metadata["reply_to_message_id"] = message.reply_to
        return OutboundMessage(
            channel_id=channel_id,
            conversation_id=conversation_id,
            message_type=str(metadata.get("imcodex_message_type") or "message"),
            text=text,
            request_id=(str(metadata.get("request_id") or "") or None),
            metadata=metadata,
            artifacts=artifacts,
        )


class SdkWebhookServiceFacade:
    """Temporary API compatibility while HTTP parsing remains product-owned."""

    def __init__(
        self,
        *,
        channel: SdkWebhookChannel,
        product_service,
        delivery_service=None,
    ) -> None:
        self.channel = channel
        self.product_service = product_service
        self.store = product_service.store
        self.backend = product_service.backend
        self.outbound_sink = channel.outbound_sink
        self.delivery_service = delivery_service

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        return await self.channel.receive(message)

    def preflight_inbound_attachments(self, message: InboundMessage):
        return self.product_service.preflight_inbound_attachments(message)

    def __getattr__(self, name: str):
        if self.delivery_service is not None and hasattr(self.delivery_service, name):
            return getattr(self.delivery_service, name)
        return getattr(self.product_service, name)

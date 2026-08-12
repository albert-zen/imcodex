from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from imagent.gateway.delivery import (
    ConversationDeliveryTarget,
    DeliveryIntent,
    DeliveryPrincipal,
    DeliverySubmissionState,
    ThreadRouteDeliveryTarget,
)
from imagent.applications import ProjectRef, ThreadRef
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
from ..delivery_outbox import DeliveryOutcome, DeliveryOutbox
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
        project_ref: ProjectRef,
        delivery_outbox: DeliveryOutbox,
        channels=(),
        delivery_authorizer=None,
        artifact_stager: DeliveryArtifactStager | None = None,
    ) -> None:
        self.channel = channel
        self.gateway = gateway
        self.client = client
        self.product_state = product_state
        self._project_ref = project_ref
        self._delivery_outbox = delivery_outbox
        self.outbound_sink = channel.outbound_sink
        self._channels = tuple(channels)
        self._channel_ids = frozenset(
            str(getattr(item, "channel_instance_id", ""))
            for item in self._channels
            if getattr(item, "channel_instance_id", None)
        )
        self._delivery_authorizer = delivery_authorizer
        self._artifact_stager = artifact_stager
        self._delivery_task: asyncio.Task[None] | None = None
        self._delivery_wake = asyncio.Event()
        self._delivery_attempt_lock = asyncio.Lock()
        self._delivery_worker_error = ""

    async def start(self) -> None:
        if self._delivery_task is not None:
            raise RuntimeError("durable delivery service is already started")
        await self._cleanup_outbound_artifacts()
        self._delivery_task = asyncio.create_task(
            self._delivery_loop(),
            name="imcodex-durable-delivery",
        )
        self._delivery_wake.set()

    async def handle_inbound(self, message: InboundMessage, **_options) -> list[OutboundMessage]:
        return await self.channel.receive(message)

    async def close(self) -> None:
        await self.stop()
        await self._cleanup_outbound_artifacts()
        await asyncio.to_thread(self._delivery_outbox.close)

    async def stop(self) -> None:
        task = self._delivery_task
        self._delivery_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def can_deliver_outbound(self, channel_id: str) -> bool:
        return channel_id in self._channel_ids or self.outbound_sink is not None

    def validate_outbound_message(self, message: OutboundMessage) -> None:
        source_thread_id = str(message.metadata.get("source_thread_id") or "").strip()
        explicit_route = bool(message.channel_id or message.conversation_id)
        if source_thread_id and explicit_route:
            raise ValueError("a source thread cannot be combined with a channel route")
        if not source_thread_id and not explicit_route:
            raise ValueError("a channel route or source thread is required")
        if explicit_route and not (message.channel_id and message.conversation_id):
            raise ValueError("channel and conversation must be provided together")
        if explicit_route and not self.can_deliver_outbound(message.channel_id):
            raise ValueError(f"Configured channel {message.channel_id!r} is unavailable.")

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
            await self._cleanup_outbound_artifacts()

    async def deliver_outbound_message(
        self,
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        self.validate_outbound_message(message)
        stage = await asyncio.to_thread(self._delivery_outbox.stage, message)
        if stage.outcome is not None:
            return self._replay_delivery_outcome(stage.outcome)
        async with self._delivery_attempt_lock:
            outcome = await asyncio.to_thread(
                self._delivery_outbox.get_outcome,
                str(message.metadata.get("delivery_id") or ""),
            )
            if outcome is not None:
                return self._replay_delivery_outcome(outcome)
            result = await self._attempt_durable_delivery(stage.pending.message)
        if not result[1] and result[2]:
            self._delivery_wake.set()
        return result

    async def _deliver_outbound_once(
        self,
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        source_thread_id = str(message.metadata.get("source_thread_id") or "").strip()
        if (
            not source_thread_id
            and message.channel_id not in self._channel_ids
            and self.outbound_sink is not None
        ):
            await self.outbound_sink.send_message(message)
            return [message], True, True
        conversation_ref = None
        thread_ref = None
        if source_thread_id:
            thread_ref = ThreadRef(self._project_ref, source_thread_id)
        else:
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
            target=(
                ThreadRouteDeliveryTarget(thread_ref)
                if thread_ref is not None
                else ConversationDeliveryTarget(conversation_ref)
            ),
            content=tuple(content),
            created_at=datetime.now(UTC),
            metadata={"imcodex_message_type": message.message_type},
        )
        credential = await self._delivery_authorizer.issue(
            DeliveryPrincipal(
                principal_id="imcodex:local-delivery",
                allowed_threads=(thread_ref,) if thread_ref is not None else (),
                allowed_conversations=(conversation_ref,) if conversation_ref is not None else (),
            )
        )
        try:
            result = await self.gateway.deliver_proactively(intent, credential=credential)
        finally:
            await self._delivery_authorizer.revoke(credential)
        message.metadata["sdk_submission_state"] = result.state.value
        if result.error:
            message.metadata["sdk_delivery_error"] = result.error
        self._record_sdk_artifact_outcomes(message, result)
        if result.state is DeliverySubmissionState.ACCEPTED:
            return [message], True, True
        if result.state is DeliverySubmissionState.PARTIAL:
            if any(
                destination.state
                in {
                    DeliverySubmissionState.IN_FLIGHT,
                    DeliverySubmissionState.RETRYABLE,
                }
                for destination in result.destinations
            ):
                return [message], False, True
            return [message], True, True
        if result.state in {
            DeliverySubmissionState.IN_FLIGHT,
            DeliverySubmissionState.RETRYABLE,
        }:
            return [message], False, True
        if result.state is DeliverySubmissionState.UNKNOWN:
            message.metadata["artifact_outcome_unknown"] = True
            return [message], False, False
        raise ValueError(result.error or "SDK proactive delivery was rejected")

    async def _attempt_durable_delivery(
        self,
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        delivery_id = str(message.metadata.get("delivery_id") or "")
        try:
            outbound, delivered, durable = await self._deliver_outbound_once(message)
        except PermissionError as exc:
            await self._complete_delivery_error(
                delivery_id,
                message,
                kind="permission",
                error=str(exc),
            )
            raise
        except ValueError as exc:
            await self._complete_delivery_error(
                delivery_id,
                message,
                kind="rejected",
                error=str(exc),
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._delivery_outbox.record_retry,
                delivery_id,
                message,
                error=f"{type(exc).__name__}: {exc}",
            )
            return [message], False, True
        final_message = outbound[-1] if outbound else message
        if delivered or not durable:
            await asyncio.to_thread(
                self._delivery_outbox.complete,
                delivery_id,
                final_message,
                delivered=delivered,
                durable=durable,
            )
            await self._cleanup_outbound_artifacts()
        else:
            await asyncio.to_thread(
                self._delivery_outbox.record_retry,
                delivery_id,
                final_message,
                error=str(final_message.metadata.get("sdk_delivery_error") or ""),
            )
        return outbound, delivered, durable

    async def _complete_delivery_error(
        self,
        delivery_id: str,
        message: OutboundMessage,
        *,
        kind: str,
        error: str,
    ) -> None:
        await asyncio.to_thread(
            self._delivery_outbox.complete,
            delivery_id,
            message,
            delivered=False,
            durable=False,
            error_kind=kind,
            error=error,
        )
        await self._cleanup_outbound_artifacts()

    async def _delivery_loop(self) -> None:
        while True:
            await self._delivery_wake.wait()
            self._delivery_wake.clear()
            while await asyncio.to_thread(self._delivery_outbox.list_pending):
                try:
                    await self._drain_pending_deliveries()
                    self._delivery_worker_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._delivery_worker_error = type(exc).__name__
                if await asyncio.to_thread(self._delivery_outbox.list_pending):
                    await asyncio.sleep(1.0)

    async def _drain_pending_deliveries(self) -> None:
        blocked_routes: set[tuple[str, str]] = set()
        async with self._delivery_attempt_lock:
            pending_entries = await asyncio.to_thread(self._delivery_outbox.list_pending)
            for pending in pending_entries:
                route = self._delivery_route_key(pending.message)
                if route in blocked_routes:
                    continue
                try:
                    _outbound, delivered, durable = await self._attempt_durable_delivery(
                        pending.message
                    )
                except (PermissionError, ValueError):
                    continue
                if not delivered and durable:
                    blocked_routes.add(route)

    async def _cleanup_outbound_artifacts(self) -> None:
        if self._artifact_stager is None:
            return
        referenced = await asyncio.to_thread(
            self._delivery_outbox.referenced_artifact_paths
        )
        await asyncio.to_thread(
            self._artifact_stager.cleanup_unreferenced,
            referenced,
        )

    def delivery_health(self) -> dict[str, object]:
        health = self._delivery_outbox.health()
        if self._delivery_worker_error:
            health.update(
                status="degraded",
                worker_error=self._delivery_worker_error,
            )
        return health

    @staticmethod
    def _record_sdk_artifact_outcomes(message: OutboundMessage, result) -> None:
        if not message.artifacts:
            return
        offset = 1 if message.text else 0
        recorded = []
        for artifact_index, artifact in enumerate(message.artifacts):
            content_index = offset + artifact_index
            items = [
                item
                for destination in result.destinations
                if destination.receipt is not None
                for item in destination.receipt.items
                if item.content_index == content_index
            ]
            if not items:
                continue
            statuses = {item.status.value for item in items}
            if statuses == {"accepted"}:
                status = "delivered"
                error = ""
            elif "rejected" in statuses:
                status = "failed"
                error = next(
                    (str(item.detail) for item in items if item.detail),
                    "delivery was rejected by the platform",
                )
            else:
                message.metadata["artifact_outcome_unknown"] = True
                continue
            recorded.append(
                {
                    "filename": artifact.filename,
                    "sha256": artifact.sha256,
                    "local_path": artifact.local_path,
                    "status": status,
                    "error": error,
                    "platform_message_id": str(
                        next(
                            (
                                item.native_message_id
                                for item in items
                                if item.native_message_id
                            ),
                            "",
                        )
                    ),
                    "delivery_identity": "",
                }
            )
        if recorded:
            message.metadata["artifact_receipts"] = recorded

    @staticmethod
    def _delivery_route_key(message: OutboundMessage) -> tuple[str, str]:
        source_thread_id = str(message.metadata.get("source_thread_id") or "").strip()
        if source_thread_id:
            return ("thread", source_thread_id)
        return (message.channel_id, message.conversation_id)

    @staticmethod
    def _replay_delivery_outcome(
        outcome: DeliveryOutcome,
    ) -> tuple[list[OutboundMessage], bool, bool]:
        if outcome.error_kind == "permission":
            raise PermissionError(outcome.error)
        if outcome.error_kind:
            raise ValueError(outcome.error)
        return [outcome.message], outcome.delivered, outcome.durable

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

from __future__ import annotations

import hashlib
import shlex
from datetime import UTC, datetime

from imagent.contracts import (
    AttachmentContent,
    BindConversationToThread,
    ClearConversationThread,
    ConversationBound,
    GetThread,
    GatewayOperationFailed,
    LocalPath,
    RequestResponseRouted,
    RespondToRequest,
    TextContent,
    TextFormat,
    ThreadRead,
    ThreadRef,
)
from imagent.contracts import (
    InboundMessage as SdkInboundMessage,
)
from imagent.contracts import (
    OutboundMessage as SdkOutboundMessage,
)

from ..models import InboundAttachment, InboundMessage, OutboundMessage
from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
)
from .sdk_requests import ImcodexRequestPresenter


class ImcodexController:
    """Product command/onboarding policy over SDK typed Gateway actions."""

    def __init__(
        self,
        *,
        service,
        request_presenter: ImcodexRequestPresenter,
        application_instance_id: str = "codex-main",
    ) -> None:
        self.service = service
        self.request_presenter = request_presenter
        self.application_instance_id = application_instance_id

    async def handle(
        self, message: SdkInboundMessage, actions
    ) -> tuple[SdkOutboundMessage, ...] | None:
        legacy = self._legacy_inbound(message)
        sdk_binding = await actions.get_binding(message.conversation_ref)
        await self._project_sdk_binding(message, legacy, sdk_binding, actions)
        product_binding = self.service.store.get_binding(
            legacy.channel_id,
            legacy.conversation_id,
        )

        request_output = await self._handle_request_command(
            message, actions, legacy.text
        )
        if request_output is not None:
            return (request_output,)

        if not legacy.text.startswith("/"):
            await self._cancel_pending_approvals(message, actions)
            if (
                product_binding.bootstrap_cwd is None
                and product_binding.thread_id is None
            ):
                outputs = await self.service.handle_inbound(legacy)
                return self._sdk_outputs(message, outputs)
            sdk_thread_id = (
                sdk_binding.thread_ref.native_thread_id
                if sdk_binding is not None and sdk_binding.thread_ref is not None
                else None
            )
            if (
                product_binding.thread_id is None
                or sdk_thread_id != product_binding.thread_id
            ):
                await self.service.backend.ensure_thread(
                    legacy.channel_id,
                    legacy.conversation_id,
                )
                await self._sync_binding(message, actions, sdk_binding)
            return None

        outputs = await self.service.handle_inbound(legacy)
        await self._sync_binding(message, actions, sdk_binding)
        return self._sdk_outputs(message, outputs)

    async def _project_sdk_binding(
        self,
        inbound: SdkInboundMessage,
        message: InboundMessage,
        binding,
        actions,
    ) -> None:
        if binding is None:
            return
        product = self.service.store.get_binding(
            message.channel_id,
            message.conversation_id,
        )
        thread_ref = binding.thread_ref
        thread_id = thread_ref.native_thread_id if thread_ref is not None else None
        if product.thread_id == thread_id:
            return
        if thread_id is None:
            self.service.store.clear_thread_binding(
                message.channel_id,
                message.conversation_id,
            )
            return
        if binding.application_ref is None:
            raise RuntimeError(
                "SDK Thread projection has no authoritative Application"
            )
        read = await actions.execute_application(
            GetThread(
                operation_id=f"imcodex:{inbound.message_id}:project-thread",
                application_ref=binding.application_ref,
                thread_ref=thread_ref,
                created_at=inbound.created_at,
            )
        )
        if not isinstance(read, ThreadRead):
            raise RuntimeError(
                "SDK Thread projection could not read the authoritative Thread"
            )
        cwd = str(read.thread.metadata.get("cwd") or "").strip()
        if not cwd:
            raise RuntimeError(
                "SDK Thread projection did not expose an authoritative cwd"
            )
        self.service.store.project_sdk_thread_context(
            message.channel_id,
            message.conversation_id,
            thread_id,
            cwd,
        )

    async def _handle_request_command(self, message, actions, text):
        try:
            parts = shlex.split(text)
        except ValueError:
            return None
        if not parts or parts[0] not in {"/approve", "/deny", "/cancel", "/answer"}:
            return None
        command = parts[0][1:]
        try:
            if command == "answer":
                request, response = self._answer_response(
                    message.conversation_ref, parts[1:]
                )
            else:
                token = parts[1] if len(parts) == 2 else None
                if len(parts) > 2:
                    raise ValueError(f"Usage: /{command} [request-id]")
                request = self.request_presenter.match(
                    message.conversation_ref,
                    token,
                    kind="approval",
                )
                response = self.request_presenter.approval_response(request, command)
            result = await actions.execute_gateway(
                RespondToRequest(
                    operation_id=f"imcodex:{message.message_id}:respond-request",
                    conversation_ref=message.conversation_ref,
                    actor=message.sender,
                    request_ref=request.request_ref,
                    response=response,
                    created_at=message.created_at,
                )
            )
            if isinstance(result, GatewayOperationFailed):
                if self._terminal_request_failure(result):
                    self.request_presenter.resolved(message.conversation_ref, request)
                raise ValueError(result.error.message)
            if not isinstance(result, RequestResponseRouted):
                raise RuntimeError(
                    "SDK request response returned an incompatible result"
                )
            self.request_presenter.resolved(message.conversation_ref, request)
            text = (
                f"Recorded answer for {request.request_ref.native_request_id}."
                if command == "answer"
                else f"Recorded {command} for {request.request_ref.native_request_id}."
            )
            return self._text_output(message, text)
        except ValueError as exc:
            return self._text_output(message, str(exc))

    def _answer_response(self, conversation_ref, arguments):
        if not arguments:
            raise ValueError("Usage: /answer <request-id> key=value ...")
        token = None if "=" in arguments[0] else arguments[0]
        answer_parts = arguments if token is None else arguments[1:]
        if not answer_parts:
            raise ValueError("Usage: /answer <request-id> key=value ...")
        answers: dict[str, list[str]] = {}
        for part in answer_parts:
            key, separator, value = part.partition("=")
            if not separator or not key or not value:
                raise ValueError("Usage: /answer <request-id> key=value ...")
            answers.setdefault(key, []).append(value)
        request = self.request_presenter.match(
            conversation_ref, token, kind="user_input"
        )
        return request, self.request_presenter.user_input_response(
            request,
            {key: tuple(values) for key, values in answers.items()},
        )

    async def _cancel_pending_approvals(self, message, actions) -> None:
        for request in self.request_presenter.approvals(message.conversation_ref):
            response = self.request_presenter.approval_response(request, "cancel")
            result = await actions.execute_gateway(
                RespondToRequest(
                    operation_id=(
                        f"imcodex:{message.message_id}:cancel:{request.request_ref.native_request_id}"
                    ),
                    conversation_ref=message.conversation_ref,
                    actor=message.sender,
                    request_ref=request.request_ref,
                    response=response,
                    created_at=message.created_at,
                )
            )
            if isinstance(result, GatewayOperationFailed):
                if self._terminal_request_failure(result):
                    self.request_presenter.resolved(message.conversation_ref, request)
                    continue
                raise RuntimeError(
                    f"Could not cancel pending approval: {result.error.message}"
                )
            self.request_presenter.resolved(message.conversation_ref, request)

    @staticmethod
    def _terminal_request_failure(result: GatewayOperationFailed) -> bool:
        return result.error.code in {
            "request_duplicate",
            "request_resolved",
            "request_stale",
        }

    @staticmethod
    def _text_output(inbound: SdkInboundMessage, text: str) -> SdkOutboundMessage:
        return SdkOutboundMessage(
            delivery_id=f"imcodex:controller:{inbound.message_id}:request",
            conversation_ref=inbound.conversation_ref,
            content=(TextContent(text, TextFormat.MARKDOWN),),
            created_at=datetime.now(UTC),
            reply_to=inbound.message_id,
            metadata={"imcodex_product_controller": True},
        )

    async def close(self) -> None:
        await self.service.close()

    async def _sync_binding(
        self,
        message: SdkInboundMessage,
        actions,
        current,
    ) -> None:
        channel_id, conversation_id = self._product_route(message)
        product = self.service.store.get_binding(
            channel_id,
            conversation_id,
        )
        expected_revision = current.revision if current is not None else None
        if product.thread_id:
            if current is not None and current.thread_ref is not None:
                if current.thread_ref.native_thread_id == product.thread_id:
                    return
            operation = BindConversationToThread(
                operation_id=f"imcodex:{message.message_id}:bind-thread",
                conversation_ref=message.conversation_ref,
                actor=message.sender,
                thread_ref=ThreadRef(self.application_instance_id, product.thread_id),
                expected_revision=expected_revision,
                created_at=message.created_at,
            )
        else:
            if current is None or current.thread_ref is None:
                return
            operation = ClearConversationThread(
                operation_id=f"imcodex:{message.message_id}:clear-thread",
                conversation_ref=message.conversation_ref,
                actor=message.sender,
                expected_revision=expected_revision,
                created_at=message.created_at,
            )
        result = await actions.execute_gateway(operation)
        if isinstance(result, GatewayOperationFailed):
            raise RuntimeError(
                f"SDK binding synchronization failed: {result.error.message}"
            )
        if not isinstance(result, ConversationBound):
            raise RuntimeError(
                "SDK binding synchronization returned an incompatible result"
            )

    @staticmethod
    def _legacy_inbound(message: SdkInboundMessage) -> InboundMessage:
        channel_id, conversation_id = ImcodexController._product_route(message)
        text = "\n".join(
            item.text for item in message.content if isinstance(item, TextContent)
        )
        attachments = tuple(
            InboundAttachment(
                kind=(
                    "image"
                    if str(item.metadata.get("kind") or "").casefold() == "image"
                    or item.media_type.startswith("image/")
                    else "file"
                ),
                content_type=item.media_type,
                local_path=item.source.path,
                size_bytes=int(item.size_bytes or 0),
                filename=str(item.filename or ""),
                source_channel_id=message.conversation_ref.channel_instance_id,
                source_message_id=item.attachment_id,
            )
            for item in message.content
            if isinstance(item, AttachmentContent)
            and isinstance(item.source, LocalPath)
        )
        return InboundMessage(
            channel_id=channel_id,
            conversation_id=conversation_id,
            user_id=message.sender,
            message_id=message.message_id,
            text=text,
            attachments=attachments,
            input_error=(str(message.metadata.get("input_error") or "") or None),
            reply_to_message_id=message.reply_to,
            sent_at=message.created_at.astimezone(UTC).isoformat(),
            trace_id=(str(message.metadata.get("trace_id") or "") or None),
        )

    @staticmethod
    def _product_route(message: SdkInboundMessage) -> tuple[str, str]:
        conversation = message.conversation_ref
        if conversation.channel_instance_id == WEBHOOK_CHANNEL_INSTANCE_ID:
            return decode_webhook_conversation(conversation.native_conversation_id)
        return (
            conversation.channel_instance_id,
            conversation.native_conversation_id,
        )

    @classmethod
    def _sdk_outputs(
        cls,
        inbound: SdkInboundMessage,
        outputs: list[OutboundMessage],
    ) -> tuple[SdkOutboundMessage, ...]:
        return tuple(
            cls._sdk_output(inbound, output, index=index)
            for index, output in enumerate(outputs)
        )

    @staticmethod
    def _sdk_output(
        inbound: SdkInboundMessage,
        output: OutboundMessage,
        *,
        index: int,
    ) -> SdkOutboundMessage:
        content: list[TextContent | AttachmentContent] = []
        if output.text:
            content.append(TextContent(output.text, TextFormat.MARKDOWN))
        for artifact in output.artifacts:
            identity = (
                artifact.sha256
                or hashlib.sha256(artifact.local_path.encode()).hexdigest()
            )
            content.append(
                AttachmentContent(
                    attachment_id=f"imcodex:artifact:{identity}",
                    media_type=artifact.content_type,
                    source=LocalPath(artifact.local_path),
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                    metadata={"kind": artifact.kind, "sha256": artifact.sha256},
                )
            )
        if not content:
            content.append(TextContent("Command completed.", TextFormat.PLAIN))
        metadata = dict(output.metadata)
        metadata.update(
            {
                "imcodex_message_type": output.message_type,
                "imcodex_product_controller": True,
            }
        )
        return SdkOutboundMessage(
            delivery_id=f"imcodex:controller:{inbound.message_id}:{index}",
            conversation_ref=inbound.conversation_ref,
            content=tuple(content),
            created_at=datetime.now(UTC),
            reply_to=inbound.message_id,
            metadata=metadata,
        )

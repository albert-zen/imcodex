from __future__ import annotations

from datetime import UTC, datetime

import pytest
from imagent.contracts import (
    ApplicationRef,
    ApprovalRequest,
    BindConversationToThread,
    ContractError,
    ConversationBinding,
    ConversationBound,
    ConversationRef,
    GatewayOperationFailed,
    GatewayOperationType,
    InboundMessage,
    RequestChoice,
    RequestRef,
    TextContent,
    ThreadRef,
)

from imcodex.bridge.sdk_controller import ImcodexController
from imcodex.bridge.sdk_requests import ImcodexRequestPresenter
from imcodex.models import ConversationBinding as ProductBinding
from imcodex.models import OutboundMessage
from imcodex.webhook_namespace import encode_webhook_conversation


class Store:
    def __init__(self, *, cwd=None, thread_id=None) -> None:
        self.binding = ProductBinding("telegram", "chat-1", thread_id, cwd)

    def get_binding(self, channel_id, conversation_id):
        assert (channel_id, conversation_id) == (
            self.binding.channel_id,
            self.binding.conversation_id,
        )
        return self.binding


class Backend:
    def __init__(self, store) -> None:
        self.store = store
        self.ensure_calls = 0

    async def ensure_thread(self, channel_id, conversation_id) -> str:
        self.ensure_calls += 1
        if self.store.binding.thread_id is None:
            self.store.binding.thread_id = "thread-created"
        return self.store.binding.thread_id


class Service:
    def __init__(self, store) -> None:
        self.store = store
        self.backend = Backend(store)
        self.handled = []

    async def handle_inbound(self, message):
        self.handled.append(message)
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="command_result",
                text="Product response",
            )
        ]

    async def close(self) -> None:
        return None


class Actions:
    def __init__(self, binding=None) -> None:
        self.binding = binding
        self.operations = []

    async def get_binding(self, conversation_ref):
        del conversation_ref
        return self.binding

    async def execute_gateway(self, operation):
        self.operations.append(operation)
        self.binding = ConversationBinding(
            conversation_ref=operation.conversation_ref,
            application_ref=ApplicationRef("codex-main"),
            thread_ref=getattr(operation, "thread_ref", None),
            revision=1,
        )
        return ConversationBound(
            operation_id=operation.operation_id,
            completed_at=datetime.now(UTC),
            type=GatewayOperationType.CONVERSATION_BIND_THREAD,
            binding=self.binding,
        )


class RequestFailureActions(Actions):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def execute_gateway(self, operation):
        self.operations.append(operation)
        return GatewayOperationFailed(
            operation_id=operation.operation_id,
            type=operation.type,
            error=ContractError(self.code, f"request failed: {self.code}"),
            completed_at=datetime.now(UTC),
        )


def _inbound(text: str) -> InboundMessage:
    return InboundMessage(
        message_id="message-1",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        sender="user-1",
        content=(TextContent(text),),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_onboarding_is_consumed_by_product_controller() -> None:
    service = Service(Store())
    controller = ImcodexController(
        service=service, request_presenter=ImcodexRequestPresenter()
    )

    outputs = await controller.handle(_inbound("hello"), Actions())

    assert outputs is not None
    assert outputs[0].content == (TextContent("Product response", "markdown"),)
    assert service.backend.ensure_calls == 0


@pytest.mark.asyncio
async def test_first_input_prepares_product_cwd_thread_then_passes_to_gateway() -> None:
    service = Service(Store(cwd="/repo"))
    controller = ImcodexController(
        service=service, request_presenter=ImcodexRequestPresenter()
    )
    actions = Actions()

    outputs = await controller.handle(_inbound("hello"), actions)

    assert outputs is None
    assert service.backend.ensure_calls == 1
    assert isinstance(actions.operations[0], BindConversationToThread)
    assert actions.operations[0].thread_ref == ThreadRef("codex-main", "thread-created")


@pytest.mark.asyncio
async def test_input_repairs_a_crash_diverged_sdk_binding_before_dispatch() -> None:
    service = Service(Store(cwd="/repo", thread_id="thread-product"))
    controller = ImcodexController(
        service=service, request_presenter=ImcodexRequestPresenter()
    )
    actions = Actions(
        ConversationBinding(
            conversation_ref=ConversationRef("telegram", "chat-1"),
            application_ref=ApplicationRef("codex-main"),
            thread_ref=ThreadRef("codex-main", "thread-sdk-old"),
            revision=4,
        )
    )

    outputs = await controller.handle(_inbound("hello"), actions)

    assert outputs is None
    assert service.backend.ensure_calls == 1
    assert actions.operations[0].expected_revision == 4
    assert actions.operations[0].thread_ref == ThreadRef("codex-main", "thread-product")


@pytest.mark.asyncio
async def test_slash_command_stays_product_owned_and_syncs_binding() -> None:
    service = Service(Store(cwd="/repo", thread_id="thread-new"))
    controller = ImcodexController(
        service=service, request_presenter=ImcodexRequestPresenter()
    )
    actions = Actions(
        ConversationBinding(
            conversation_ref=ConversationRef("telegram", "chat-1"),
            application_ref=ApplicationRef("codex-main"),
            thread_ref=ThreadRef("codex-main", "thread-old"),
            revision=3,
        )
    )

    outputs = await controller.handle(_inbound("/status"), actions)

    assert outputs is not None
    assert outputs[0].metadata["imcodex_product_controller"] is True
    assert actions.operations[0].expected_revision == 3
    assert actions.operations[0].thread_ref.native_thread_id == "thread-new"


@pytest.mark.asyncio
async def test_generic_webhook_controller_uses_original_product_namespace() -> None:
    store = Store()
    store.binding.channel_id = "custom-a"
    store.binding.conversation_id = "room/1"
    service = Service(store)
    controller = ImcodexController(
        service=service,
        request_presenter=ImcodexRequestPresenter(),
    )
    inbound = InboundMessage(
        message_id="message-1",
        conversation_ref=ConversationRef(
            "webhook",
            encode_webhook_conversation("custom-a", "room/1"),
        ),
        sender="user-1",
        content=(TextContent("hello"),),
        created_at=datetime.now(UTC),
    )

    outputs = await controller.handle(inbound, Actions())

    assert outputs is not None
    assert service.handled[0].channel_id == "custom-a"
    assert service.handled[0].conversation_id == "room/1"


def _present_approval(presenter: ImcodexRequestPresenter) -> ApprovalRequest:
    request = ApprovalRequest(
        request_ref=RequestRef(ApplicationRef("codex-main"), "request-approval"),
        thread_ref=ThreadRef("codex-main", "thread-1"),
        turn_id="turn-1",
        prompt="Run?",
        choices=(RequestChoice("accept", "Approve"), RequestChoice("decline", "Deny")),
    )
    presenter.present_request(
        request,
        conversation_ref=ConversationRef("telegram", "chat-1"),
        delivery_id="delivery-request",
        reply_to_message_id="message-0",
    )
    return request


@pytest.mark.asyncio
async def test_terminal_stale_approval_handle_cannot_block_replayed_input() -> None:
    presenter = ImcodexRequestPresenter()
    _present_approval(presenter)
    service = Service(Store())
    controller = ImcodexController(service=service, request_presenter=presenter)

    outputs = await controller.handle(
        _inbound("hello"), RequestFailureActions("request_stale")
    )

    assert outputs is not None
    assert service.handled
    assert presenter.approvals(ConversationRef("telegram", "chat-1")) == ()


@pytest.mark.asyncio
async def test_transient_approval_cancel_failure_retains_handle_and_blocks_input() -> (
    None
):
    presenter = ImcodexRequestPresenter()
    request = _present_approval(presenter)
    service = Service(Store())
    controller = ImcodexController(service=service, request_presenter=presenter)

    with pytest.raises(RuntimeError, match="adapter_failure"):
        await controller.handle(
            _inbound("hello"), RequestFailureActions("adapter_failure")
        )

    assert service.handled == []
    assert presenter.approvals(ConversationRef("telegram", "chat-1")) == (request,)

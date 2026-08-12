from __future__ import annotations

from types import SimpleNamespace

from imagent import ProjectionPolicy
from imagent.applications import ProjectRef, ThreadRef
from imagent.gateway.delivery import (
    DeliverySubmissionState,
    ScopedDeliveryAuthorizer,
    ThreadRouteDeliveryTarget,
)

from imcodex.channels.sdk_webhook import SdkRuntimeService, SdkWebhookChannel
from imcodex.models import OutboundMessage
from imcodex.sdk_composition import SDK_PROJECTION_POLICY


class _Gateway:
    def __init__(self, authorizer: ScopedDeliveryAuthorizer) -> None:
        self.authorizer = authorizer
        self.intent = None
        self.principal = None

    async def deliver_proactively(self, intent, *, credential):
        self.intent = intent
        self.principal = await self.authorizer.authenticate(credential)
        return SimpleNamespace(state=DeliverySubmissionState.ACCEPTED, error=None)


def test_sdk_composition_remembers_last_thread_recipient() -> None:
    assert SDK_PROJECTION_POLICY is ProjectionPolicy.REMEMBERED_LAST_RECIPIENT


async def test_current_thread_delivery_uses_sdk_thread_route_target() -> None:
    authorizer = ScopedDeliveryAuthorizer()
    gateway = _Gateway(authorizer)
    project_ref = ProjectRef("codex-main", "imcodex-workspace")
    service = SdkRuntimeService(
        channel=SdkWebhookChannel(),
        gateway=gateway,
        client=SimpleNamespace(),
        product_state=SimpleNamespace(),
        project_ref=project_ref,
        delivery_authorizer=authorizer,
    )
    message = OutboundMessage(
        channel_id="",
        conversation_id="",
        message_type="tool_delivery",
        text="artifact ready",
        metadata={
            "delivery_id": "delivery-1",
            "source_thread_id": "thread-1",
        },
    )

    outbound, delivered, durable = await service.deliver_outbound_message(message)

    thread_ref = ThreadRef(project_ref, "thread-1")
    assert isinstance(gateway.intent.target, ThreadRouteDeliveryTarget)
    assert gateway.intent.target.thread_ref == thread_ref
    assert gateway.principal.allowed_threads == (thread_ref,)
    assert gateway.principal.allowed_conversations == ()
    assert outbound == [message]
    assert delivered is True
    assert durable is True

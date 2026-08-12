from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from imagent import ProjectionPolicy
from imagent.applications import ProjectRef, ThreadRef
from imagent.gateway.delivery import (
    DeliverySubmissionState,
    ScopedDeliveryAuthorizer,
    ThreadRouteDeliveryTarget,
)
from imagent.interaction.channels import (
    DeliveryItemReceipt,
    DeliveryItemStatus,
    DeliveryReceipt,
    DeliveryReceiptStatus,
)

from imcodex.channels.sdk_webhook import SdkRuntimeService, SdkWebhookChannel
from imcodex.delivery_artifacts import DeliveryArtifactStager
from imcodex.delivery_outbox import DeliveryOutbox
from imcodex.models import OutboundMessage
from imcodex.sdk_composition import SDK_PROJECTION_POLICY


class _Gateway:
    def __init__(
        self,
        authorizer: ScopedDeliveryAuthorizer,
        states=(DeliverySubmissionState.ACCEPTED,),
    ) -> None:
        self.authorizer = authorizer
        self.states = list(states)
        self.intent = None
        self.principal = None
        self.calls = 0

    async def deliver_proactively(self, intent, *, credential):
        self.calls += 1
        self.intent = intent
        self.principal = await self.authorizer.authenticate(credential)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        destinations = ()
        if state is DeliverySubmissionState.PARTIAL:
            destinations = (
                SimpleNamespace(
                    state=DeliverySubmissionState.PARTIAL,
                    receipt=DeliveryReceipt(
                        status=DeliveryReceiptStatus.REJECTED_BY_PLATFORM,
                        items=(
                            DeliveryItemReceipt(
                                content_index=0,
                                status=DeliveryItemStatus.ACCEPTED,
                            ),
                            DeliveryItemReceipt(
                                content_index=1,
                                status=DeliveryItemStatus.REJECTED,
                                detail="file type rejected",
                            ),
                        ),
                    )
                ),
            )
        return SimpleNamespace(state=state, error=None, destinations=destinations)


def _service(tmp_path, states, *, artifact_stager=None):
    authorizer = ScopedDeliveryAuthorizer()
    gateway = _Gateway(authorizer, states)
    service = SdkRuntimeService(
        channel=SdkWebhookChannel(),
        gateway=gateway,
        client=SimpleNamespace(),
        product_state=SimpleNamespace(),
        project_ref=ProjectRef("codex-main", "imcodex-workspace"),
        delivery_outbox=DeliveryOutbox(tmp_path / "delivery.sqlite3"),
        delivery_authorizer=authorizer,
        artifact_stager=artifact_stager,
    )
    return service, gateway


def _thread_message(delivery_id="delivery-1", *, artifacts=()):
    return OutboundMessage(
        channel_id="",
        conversation_id="",
        message_type="tool_delivery",
        text="artifact ready",
        metadata={
            "delivery_id": delivery_id,
            "source_thread_id": "thread-1",
        },
        artifacts=list(artifacts),
    )


def test_sdk_composition_remembers_last_thread_recipient() -> None:
    assert SDK_PROJECTION_POLICY is ProjectionPolicy.REMEMBERED_LAST_RECIPIENT


async def test_current_thread_delivery_uses_sdk_thread_route_target(tmp_path) -> None:
    authorizer = ScopedDeliveryAuthorizer()
    gateway = _Gateway(authorizer)
    project_ref = ProjectRef("codex-main", "imcodex-workspace")
    service = SdkRuntimeService(
        channel=SdkWebhookChannel(),
        gateway=gateway,
        client=SimpleNamespace(),
        product_state=SimpleNamespace(),
        project_ref=project_ref,
        delivery_outbox=DeliveryOutbox(tmp_path / "delivery.sqlite3"),
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
    assert outbound[0].metadata["delivery_id"] == "delivery-1"
    assert outbound[0].metadata["sdk_submission_state"] == "accepted"
    assert delivered is True
    assert durable is True
    await service.close()


async def test_terminal_outcome_is_replayed_without_second_sdk_submission(tmp_path) -> None:
    service, gateway = _service(tmp_path, [DeliverySubmissionState.ACCEPTED])
    message = _thread_message()

    first = await service.deliver_outbound_message(message)
    replay = await service.deliver_outbound_message(message)

    assert first[1:] == (True, True)
    assert replay[1:] == (True, True)
    assert gateway.calls == 1
    await service.close()


async def test_unknown_outcome_is_terminal_and_replayed_without_resend(tmp_path) -> None:
    service, gateway = _service(tmp_path, [DeliverySubmissionState.UNKNOWN])
    message = _thread_message()

    first = await service.deliver_outbound_message(message)
    replay = await service.deliver_outbound_message(message)

    assert first[1:] == (False, False)
    assert replay[1:] == (False, False)
    assert gateway.calls == 1
    assert service.delivery_health()["pending_count"] == 0
    await service.close()


async def test_pending_delivery_drains_after_service_restart(tmp_path) -> None:
    first_service, first_gateway = _service(
        tmp_path,
        [DeliverySubmissionState.RETRYABLE],
    )
    queued = await first_service.deliver_outbound_message(_thread_message())
    assert queued[1:] == (False, True)
    assert first_service.delivery_health()["status"] == "degraded"
    assert first_gateway.calls == 1
    await first_service.close()

    restarted, restarted_gateway = _service(
        tmp_path,
        [DeliverySubmissionState.ACCEPTED],
    )
    await restarted.start()
    await _wait_until(lambda: restarted.delivery_health()["pending_count"] == 0)

    assert restarted_gateway.calls == 1
    assert restarted.delivery_health()["status"] == "healthy"
    await restarted.close()


async def test_pending_artifact_survives_restart_until_terminal_ack(tmp_path) -> None:
    artifact_stager = DeliveryArtifactStager(tmp_path / "outbound-media")
    artifact = artifact_stager.stage_upload(
        b"artifact",
        kind="file",
        content_type="text/plain",
        filename="result.txt",
    )
    first_service, _gateway = _service(
        tmp_path,
        [DeliverySubmissionState.RETRYABLE],
        artifact_stager=artifact_stager,
    )
    await first_service.deliver_outbound_message(
        _thread_message(artifacts=(artifact,))
    )
    await first_service.discard_outbound_uploads((artifact,))
    await first_service.close()

    assert Path(artifact.local_path).exists()

    restarted_stager = DeliveryArtifactStager(tmp_path / "outbound-media")
    restarted, restarted_gateway = _service(
        tmp_path,
        [DeliverySubmissionState.ACCEPTED],
        artifact_stager=restarted_stager,
    )
    await restarted.start()
    await _wait_until(lambda: restarted.delivery_health()["pending_count"] == 0)

    assert restarted_gateway.calls == 1
    assert not Path(artifact.local_path).exists()
    await restarted.close()


async def test_partial_artifact_outcome_is_durable_and_cleaned_after_ack(tmp_path) -> None:
    stager = DeliveryArtifactStager(tmp_path / "outbound-media")
    artifact = stager.stage_upload(
        b"artifact",
        kind="file",
        content_type="text/plain",
        filename="result.txt",
    )
    service, gateway = _service(
        tmp_path,
        [DeliverySubmissionState.PARTIAL],
        artifact_stager=stager,
    )

    outcome = await service.deliver_outbound_message(
        _thread_message(artifacts=(artifact,))
    )

    assert outcome[1:] == (True, True)
    assert gateway.calls == 1
    assert Path(artifact.local_path).exists()
    receipt = outcome[0][0].metadata["artifact_receipts"][0]
    assert receipt["status"] == "failed"
    assert receipt["error"] == "file type rejected"

    replay = await service.deliver_outbound_message(
        _thread_message(artifacts=(artifact,))
    )
    await service.discard_outbound_uploads((artifact,))

    assert replay[0][0].metadata["artifact_receipts"][0]["status"] == "failed"
    assert gateway.calls == 1
    assert service.delivery_health()["pending_count"] == 0
    assert not Path(artifact.local_path).exists()
    await service.close()


async def test_partial_with_retryable_destination_remains_pending(tmp_path) -> None:
    service, gateway = _service(tmp_path, [DeliverySubmissionState.ACCEPTED])
    result = SimpleNamespace(
        state=DeliverySubmissionState.PARTIAL,
        error="one destination remains retryable",
        destinations=(
            SimpleNamespace(
                state=DeliverySubmissionState.ACCEPTED,
                receipt=None,
            ),
            SimpleNamespace(
                state=DeliverySubmissionState.RETRYABLE,
                receipt=None,
            ),
        ),
    )

    async def deliver(_intent, *, credential):
        gateway.calls += 1
        await gateway.authorizer.authenticate(credential)
        return result

    gateway.deliver_proactively = deliver

    queued = await service.deliver_outbound_message(_thread_message())

    assert queued[1:] == (False, True)
    assert service.delivery_health()["pending_count"] == 1
    await service.close()


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)

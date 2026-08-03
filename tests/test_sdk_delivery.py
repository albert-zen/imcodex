from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from imagent.adapters import DeliverySubmissionConflict
from imagent.bindings import InMemoryBindingRepository
from imagent.contracts import (
    ApplicationRef,
    AttachmentContent,
    AttachmentSourceKind,
    ChannelCapabilities,
    ConversationBinding,
    ConversationRef,
    DeliveryItemReceipt,
    DeliveryItemStatus,
    DeliveryReceipt,
    DeliveryReceiptStatus,
    DeliverySubmissionState,
    LocalPath,
    ProjectionPolicy,
    SupportLevel,
    TextContent,
    ThreadProjectionRoute,
    ThreadRef,
)
from imagent.contracts import (
    OutboundMessage as SdkOutboundMessage,
)
from imagent.delivery_outcomes import DeliveryOutcome, DeliveryOutcomeContext
from imagent.gateway import GatewayRepositories, ImAgentGateway
from imagent.proactive_delivery import ScopedDeliveryAuthorizer
from imagent.projections import InMemoryProjectionRouteRepository
from imagent.testing import FakeAgentApplicationAdapter, FakeChannelAdapter

from imcodex.bridge.outbound_artifacts import (
    OutboundArtifactLeaseLedger,
    OutboundArtifactStager,
)
from imcodex.bridge.sdk_delivery import (
    ImcodexDeliveryOutcomeObserver,
    ImcodexProactiveDelivery,
)
from imcodex.channels.sdk_webhook import SdkWebhookChannel
from imcodex.models import OutboundMessage


class _ProductStore:
    pass


class _Stager:
    def __init__(self) -> None:
        self.released = None
        self.referenced = None

    def release(self, artifacts) -> None:
        self.released = tuple(artifacts)

    def cleanup_unreferenced(self, referenced) -> None:
        self.referenced = referenced


class _Ledger:
    def __init__(self) -> None:
        self.completed = None
        self.transferred = None

    def transfer(self, delivery_id, artifacts) -> bool:
        self.transferred = (delivery_id, tuple(artifacts))
        return True

    def complete_attempt(self, attempt_id, paths) -> None:
        self.completed = (attempt_id, tuple(paths))

    def complete_delivery(self, delivery_id, paths) -> None:
        self.completed = (delivery_id, tuple(paths))

    async def stage_upload(self, content, **kwargs):
        raise AssertionError("this test has no uploads")

    def discard_request(self, artifacts) -> None:
        return None


@pytest.mark.asyncio
async def test_product_proactive_delivery_uses_sdk_gateway() -> None:
    channel = FakeChannelAdapter("telegram")
    channel._capabilities = ChannelCapabilities(
        attachments=SupportLevel.NATIVE,
        attachment_sources=(AttachmentSourceKind.LOCAL_PATH,),
    )
    authorizer = ScopedDeliveryAuthorizer()
    gateway = ImAgentGateway(
        channels=[channel],
        applications=[FakeAgentApplicationAdapter()],
        repositories=GatewayRepositories(bindings=InMemoryBindingRepository()),
        delivery_authorizer=authorizer,
    )
    delivery = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=authorizer,
        artifact_ledger=_Ledger(),
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram", "qq", "feishu", "weixin"},
        webhook_channel=SdkWebhookChannel(),
    )
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="tool_delivery",
        text="Done",
        metadata={"delivery_id": "delivery-1"},
    )

    outbound, delivered, durable = await delivery.deliver_outbound_message(message)

    assert len(outbound) == 1
    assert (outbound[0].channel_id, outbound[0].conversation_id) == (
        "telegram",
        "chat-1",
    )
    assert outbound[0].metadata["destination_state"] == "accepted"
    assert delivered is True
    assert durable is True
    assert len(channel.sent) == 1
    assert channel.sent[0].conversation_ref.native_conversation_id == "chat-1"
    assert channel.sent[0].content == (TextContent("Done"),)


@pytest.mark.asyncio
async def test_thread_delivery_fans_out_only_to_current_foreground_bindings() -> None:
    channel = FakeChannelAdapter("telegram")
    bindings = InMemoryBindingRepository()
    projections = InMemoryProjectionRouteRepository()
    thread = ThreadRef("fake-agent", "thread-1")
    switched_thread = ThreadRef("fake-agent", "thread-2")
    current_a = ConversationRef("telegram", "chat-a")
    current_b = ConversationRef("telegram", "chat-b")
    switched = ConversationRef("telegram", "chat-switched")
    for conversation, selected in (
        (current_a, thread),
        (current_b, thread),
        (switched, switched_thread),
    ):
        await bindings.put(
            ConversationBinding(
                conversation_ref=conversation,
                application_ref=ApplicationRef("fake-agent"),
                thread_ref=selected,
            )
        )
        await projections.put_projection_route(
            ThreadProjectionRoute(
                route_id=f"route:{conversation.native_conversation_id}",
                thread_ref=thread,
                conversation_ref=conversation,
            )
        )
    authorizer = ScopedDeliveryAuthorizer()
    gateway = ImAgentGateway(
        channels=[channel],
        applications=[FakeAgentApplicationAdapter()],
        repositories=GatewayRepositories(
            bindings=bindings,
            projections=projections,
        ),
        projection_policy=ProjectionPolicy.FOREGROUND_ONLY,
        delivery_authorizer=authorizer,
    )
    delivery = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=authorizer,
        artifact_ledger=_Ledger(),
        application_instance_id="fake-agent",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )

    outbound, delivered, durable = await delivery.deliver_outbound_message(
        OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="tool_delivery",
            text="Fan out",
            metadata={
                "delivery_id": "delivery-thread-1",
                "source_thread_id": "thread-1",
            },
        )
    )

    assert delivered is True
    assert durable is True
    assert len(outbound) == 1
    assert outbound[0].metadata["destination_count"] == 2
    assert outbound[0].metadata["destination_states"] == ("accepted", "accepted")
    assert {
        item.conversation_ref.native_conversation_id for item in channel.sent
    } == {"chat-a", "chat-b"}


@pytest.mark.asyncio
async def test_delivery_outcome_observer_releases_local_paths() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(
        artifact_ledger=ledger,
    )
    message = SdkOutboundMessage(
        delivery_id="delivery-1",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
                metadata={"sha256": "a" * 64},
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM)
        ),
    )

    assert ledger.completed == ("delivery-1", ("/spool/result.txt",))


@pytest.mark.asyncio
async def test_delivery_outcome_observer_retains_lease_for_retryable_attempt() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(
        artifact_ledger=ledger,
    )
    message = SdkOutboundMessage(
        delivery_id="delivery-retryable",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(status=DeliveryReceiptStatus.RETRYABLE_FAILURE)
        ),
    )

    assert ledger.completed is None


@pytest.mark.asyncio
async def test_delivery_outcome_observer_releases_terminal_unknown_items() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(artifact_ledger=ledger)
    message = SdkOutboundMessage(
        delivery_id="delivery-unknown",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(
                status=DeliveryReceiptStatus.UNKNOWN,
                items=(
                    DeliveryItemReceipt(
                        0,
                        DeliveryItemStatus.UNKNOWN,
                        "platform outcome unknown",
                    ),
                ),
            )
        ),
    )

    assert ledger.completed == ("delivery-unknown", ("/spool/result.txt",))


@pytest.mark.asyncio
async def test_delivery_outcome_observer_releases_only_terminal_partial_items() -> None:
    ledger = _Ledger()
    observer = ImcodexDeliveryOutcomeObserver(artifact_ledger=ledger)
    message = SdkOutboundMessage(
        delivery_id="delivery-partial",
        conversation_ref=SimpleNamespace(),
        content=(
            AttachmentContent(
                attachment_id="accepted",
                media_type="text/plain",
                source=LocalPath("/spool/accepted.txt"),
            ),
            AttachmentContent(
                attachment_id="retryable",
                media_type="text/plain",
                source=LocalPath("/spool/retryable.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
    )

    await observer.observe_delivery_outcome(
        DeliveryOutcomeContext(message),
        DeliveryOutcome(
            receipt=DeliveryReceipt(
                status=DeliveryReceiptStatus.ACCEPTED_BY_PLATFORM,
                items=(
                    DeliveryItemReceipt(0, DeliveryItemStatus.ACCEPTED, "accepted"),
                    DeliveryItemReceipt(
                        1,
                        DeliveryItemStatus.RETRYABLE_FAILURE,
                        "retryable",
                    ),
                ),
            )
        ),
    )

    assert ledger.completed == (
        "delivery-partial",
        ("/spool/accepted.txt",),
    )


@pytest.mark.parametrize(
    ("state", "receipt_status", "item_status"),
    (
        (
            DeliverySubmissionState.REJECTED,
            DeliveryReceiptStatus.REJECTED_BY_PLATFORM,
            DeliveryItemStatus.SKIPPED,
        ),
        (
            DeliverySubmissionState.UNKNOWN,
            DeliveryReceiptStatus.UNKNOWN,
            DeliveryItemStatus.UNKNOWN,
        ),
    ),
)
def test_terminal_proactive_result_releases_all_paths_despite_item_status(
    state,
    receipt_status,
    item_status,
) -> None:
    ledger = _Ledger()
    delivery = ImcodexProactiveDelivery(
        gateway=None,
        authorizer=ScopedDeliveryAuthorizer(),
        artifact_ledger=ledger,
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )
    content = (
        AttachmentContent(
            attachment_id="artifact-1",
            media_type="text/plain",
            source=LocalPath("/spool/result.txt"),
        ),
    )
    result = SimpleNamespace(
        destinations=(
            SimpleNamespace(
                state=state,
                receipt=DeliveryReceipt(
                    status=receipt_status,
                    items=(
                        DeliveryItemReceipt(
                            0,
                            item_status,
                            "terminal whole-attempt result",
                        ),
                    ),
                ),
            ),
        ),
    )

    delivery._reconcile_terminal_result("delivery-terminal", result, content)

    assert ledger.completed == ("delivery-terminal", ("/spool/result.txt",))


def test_mixed_fanout_retains_root_lease_until_every_destination_is_terminal(
    tmp_path,
) -> None:
    class ProductStore(_ProductStore):
        @staticmethod
        def referenced_legacy_artifact_paths() -> set[str]:
            return set()

    ledger = OutboundArtifactLeaseLedger(
        stager=OutboundArtifactStager(tmp_path / "spool"),
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    artifact = ledger.stager.stage_upload(
        b"fanout",
        kind="file",
        content_type="text/plain",
        filename="result.txt",
    )
    assert ledger.transfer("delivery-fanout", (artifact,)) is True
    delivery = ImcodexProactiveDelivery(
        gateway=None,
        authorizer=ScopedDeliveryAuthorizer(),
        artifact_ledger=ledger,
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )
    content = (
        AttachmentContent(
            attachment_id="artifact-1",
            media_type="text/plain",
            source=LocalPath(artifact.local_path),
        ),
    )

    delivery._reconcile_terminal_result(
        "delivery-fanout",
        SimpleNamespace(
            destinations=(
                SimpleNamespace(state=DeliverySubmissionState.ACCEPTED),
                SimpleNamespace(state=DeliverySubmissionState.RETRYABLE),
            )
        ),
        content,
    )

    assert Path(artifact.local_path).exists()
    assert ledger.referenced_paths() == {artifact.local_path}

    delivery._reconcile_terminal_result(
        "delivery-fanout",
        SimpleNamespace(
            destinations=(
                SimpleNamespace(state=DeliverySubmissionState.ACCEPTED),
                SimpleNamespace(state=DeliverySubmissionState.REJECTED),
            )
        ),
        content,
    )

    assert not Path(artifact.local_path).exists()
    assert ledger.referenced_paths() == set()


@pytest.mark.asyncio
async def test_terminal_proactive_replay_releases_each_new_staging_lease(
    tmp_path,
) -> None:
    class ProductStore(_ProductStore):
        @staticmethod
        def referenced_legacy_artifact_paths() -> set[str]:
            return set()

    channel = FakeChannelAdapter("telegram")
    channel._capabilities = ChannelCapabilities(
        attachments=SupportLevel.NATIVE,
        attachment_sources=(AttachmentSourceKind.LOCAL_PATH,),
    )
    authorizer = ScopedDeliveryAuthorizer()
    ledger = OutboundArtifactLeaseLedger(
        stager=OutboundArtifactStager(tmp_path / "spool"),
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    gateway = ImAgentGateway(
        channels=[channel],
        applications=[FakeAgentApplicationAdapter()],
        repositories=GatewayRepositories(bindings=InMemoryBindingRepository()),
        delivery_authorizer=authorizer,
    )
    delivery = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=authorizer,
        artifact_ledger=ledger,
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )

    for _ in range(2):
        artifact = ledger.stager.stage_upload(
            b"same replay bytes",
            kind="file",
            content_type="text/plain",
            filename="result.txt",
        )
        message = OutboundMessage(
            channel_id="telegram",
            conversation_id="chat-1",
            message_type="tool_delivery",
            text="",
            artifacts=[artifact],
            metadata={"delivery_id": "delivery-replay"},
        )
        await delivery.deliver_outbound_message(message)
        assert ledger.referenced_paths() == set()
        assert not Path(artifact.local_path).exists()

    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_delivery_conflict_preserves_an_existing_artifact_lease(tmp_path) -> None:
    class ProductStore(_ProductStore):
        @staticmethod
        def referenced_legacy_artifact_paths() -> set[str]:
            return set()

    class ConflictGateway:
        async def deliver_proactively(self, intent, *, credential):
            del intent, credential
            raise DeliverySubmissionConflict("payload conflicts")

    authorizer = ScopedDeliveryAuthorizer()
    ledger = OutboundArtifactLeaseLedger(
        stager=OutboundArtifactStager(tmp_path / "spool"),
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    artifact = ledger.stager.stage_upload(
        b"in flight",
        kind="file",
        content_type="text/plain",
        filename="result.txt",
    )
    ledger.transfer("delivery-conflict", (artifact,))
    delivery = ImcodexProactiveDelivery(
        gateway=ConflictGateway(),
        authorizer=authorizer,
        artifact_ledger=ledger,
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="tool_delivery",
        text="different payload",
        artifacts=[artifact],
        metadata={"delivery_id": "delivery-conflict"},
    )

    with pytest.raises(ValueError, match="payload conflicts"):
        await delivery.deliver_outbound_message(message)

    assert artifact.local_path in ledger.referenced_paths()
    assert Path(artifact.local_path).exists()


@pytest.mark.asyncio
async def test_artifact_transfer_failure_still_revokes_scoped_credential() -> None:
    class Authorizer:
        def __init__(self) -> None:
            self.credential = object()
            self.revoked = []

        async def issue(self, principal):
            del principal
            return self.credential

        async def revoke(self, credential):
            self.revoked.append(credential)

    class Ledger(_Ledger):
        def transfer(self, delivery_id, artifacts) -> bool:
            del delivery_id, artifacts
            raise ValueError("artifact ledger is full")

    class Gateway:
        async def deliver_proactively(self, intent, *, credential):
            raise AssertionError("transfer failure must precede Gateway delivery")

    authorizer = Authorizer()
    delivery = ImcodexProactiveDelivery(
        gateway=Gateway(),
        authorizer=authorizer,
        artifact_ledger=Ledger(),
        application_instance_id="codex-main",
        registered_channel_ids={"telegram"},
        fallback_excluded_channel_ids={"telegram"},
        webhook_channel=SdkWebhookChannel(),
    )
    message = OutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="tool_delivery",
        text="Done",
        metadata={"delivery_id": "delivery-ledger-full"},
    )

    with pytest.raises(ValueError, match="ledger is full"):
        await delivery.deliver_outbound_message(message)

    assert authorizer.revoked == [authorizer.credential]

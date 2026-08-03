from __future__ import annotations

import base64
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from imagent.applications import (
    AppServerArtifactCandidate,
    AppServerArtifactSourceKind,
    AppServerCompletedItemFacts,
    AppServerCompletedItemKind,
    AppServerCompletedItemPhase,
    AppServerTurnTerminalFacts,
    AppServerTurnTerminalStatus,
    CodexLiveActivityFacts,
    CodexLiveActivityKind,
    CodexLiveActivityMethod,
    CodexPlanStep,
)
from imagent.contracts import (
    AttachmentContent,
    ConversationRef,
    LocalPath,
    OutboundMessage,
    TextContent,
    ThreadRef,
)
from imagent.outbound_presentation import (
    OutboundPresentationContext,
    ProjectionPresentationOrigin,
)
from PIL import Image

from imcodex.bridge.outbound_artifacts import OutboundArtifactStager
from imcodex.bridge.sdk_presentation import (
    ImcodexArtifactMaterializer,
    ImcodexCodexLiveActivityPresenter,
    ImcodexOutboundPresentation,
)
from imcodex.models import OutboundArtifact
from imcodex.webhook_namespace import encode_webhook_conversation


class FakeStore:
    def __init__(self, **visibility) -> None:
        self.binding_reads = []
        self.binding = SimpleNamespace(
            bootstrap_cwd="/repo",
            show_commentary=visibility.get("show_commentary", True),
            show_toolcalls=visibility.get("show_toolcalls", False),
            show_system=visibility.get("show_system", False),
        )

    def get_binding(self, channel_id, conversation_id):
        self.binding_reads.append((channel_id, conversation_id))
        return self.binding


class FakeStager:
    def stage_appserver_candidate(self, candidate):
        assert candidate.locator == "file:///native/output.png"
        return OutboundArtifact(
            kind="image",
            local_path="/spool/output.png",
            content_type="image/png",
            filename="output.png",
            size_bytes=3,
            sha256="abc123",
        )


class FakeArtifactLedger:
    def __init__(self) -> None:
        self.tracked = []
        self.released = []

    def track_attempt(self, attempt_id, paths) -> None:
        self.tracked.append((attempt_id, tuple(paths)))

    def release_untracked(self, paths) -> None:
        self.released.append(tuple(paths))


class OverflowStager:
    def __init__(self) -> None:
        self.count = 0
        self.released = []

    def stage_appserver_candidate(self, candidate):
        del candidate
        self.count += 1
        return OutboundArtifact(
            kind="file",
            local_path=f"/spool/output-{self.count}.txt",
            content_type="text/plain",
            filename=f"output-{self.count}.txt",
            size_bytes=1,
            sha256=f"sha-{self.count}",
        )

    def release(self, artifacts) -> None:
        self.released.extend(artifact.local_path for artifact in artifacts)


def _item(
    *,
    item_id: str = "answer-1",
    authoritative: bool = False,
    kind: AppServerCompletedItemKind = AppServerCompletedItemKind.AGENT_MESSAGE,
    phase: AppServerCompletedItemPhase = AppServerCompletedItemPhase.FINAL_ANSWER,
    candidates=(),
) -> AppServerCompletedItemFacts:
    return AppServerCompletedItemFacts(
        item_id=item_id,
        thread_ref=ThreadRef("codex-main", "thread-1"),
        turn_id="turn-1",
        authoritative=authoritative,
        kind=kind,
        phase=phase,
        has_default_message=(kind is AppServerCompletedItemKind.AGENT_MESSAGE),
        artifact_candidates=tuple(candidates),
    )


def _terminal(*, authoritative: bool = False) -> AppServerTurnTerminalFacts:
    return AppServerTurnTerminalFacts(
        thread_ref=ThreadRef("codex-main", "thread-1"),
        turn_id="turn-1",
        authoritative=authoritative,
        status=AppServerTurnTerminalStatus.COMPLETED,
    )


def _candidate() -> AppServerArtifactCandidate:
    return AppServerArtifactCandidate(
        candidate_id="tool-1:image:0",
        source_kind=AppServerArtifactSourceKind.FILE_URL,
        locator="file:///native/output.png",
    )


def _outbound(kind: str, *, phase: str = "") -> OutboundMessage:
    return OutboundMessage(
        delivery_id=f"delivery-{kind}-{phase}",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(TextContent("message"),),
        created_at=datetime.now(UTC),
        metadata={
            "native_application": "codex",
            "native_item_kind": kind,
            "phase": phase,
        },
    )


@pytest.mark.asyncio
async def test_artifact_candidate_is_attached_to_the_next_final_answer() -> None:
    materializer = ImcodexArtifactMaterializer(artifact_stager=FakeStager())

    assert (
        await materializer.materialize_completed_item(
            _item(
                item_id="tool-1",
                kind=AppServerCompletedItemKind.DYNAMIC_TOOL_CALL,
                phase=AppServerCompletedItemPhase.NONE,
                candidates=(_candidate(),),
            )
        )
        is None
    )
    projected = await materializer.materialize_completed_item(_item())

    assert projected is not None
    assert isinstance(projected.attachments[0], AttachmentContent)
    assert projected.attachments[0].attachment_id == "imcodex:artifact:abc123"
    assert await materializer.materialize_turn_terminal(_terminal()) is None


@pytest.mark.asyncio
async def test_artifact_only_terminal_fallback_and_authoritative_scope() -> None:
    materializer = ImcodexArtifactMaterializer(artifact_stager=FakeStager())
    await materializer.materialize_completed_item(
        _item(
            item_id="tool-1",
            kind=AppServerCompletedItemKind.DYNAMIC_TOOL_CALL,
            phase=AppServerCompletedItemPhase.NONE,
            candidates=(_candidate(),),
        )
    )

    assert (
        await materializer.materialize_turn_terminal(_terminal(authoritative=True))
        is None
    )
    fallback = await materializer.materialize_turn_terminal(_terminal())
    assert fallback is not None
    assert fallback.attachments[0].attachment_id == "imcodex:artifact:abc123"


@pytest.mark.asyncio
async def test_authoritative_a1_replay_recreates_content_addressed_artifact(
    tmp_path: Path,
) -> None:
    stream = BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(stream, format="PNG")
    candidate = AppServerArtifactCandidate(
        candidate_id="tool-1:image:0",
        source_kind=AppServerArtifactSourceKind.DATA_URL,
        locator="data:image/png;base64,"
        + base64.b64encode(stream.getvalue()).decode("ascii"),
    )
    facts = _item(authoritative=True, candidates=(candidate,))
    first = ImcodexArtifactMaterializer(
        artifact_stager=OutboundArtifactStager(tmp_path / "spool")
    )
    materialized = await first.materialize_completed_item(facts)
    assert materialized is not None
    path = Path(materialized.attachments[0].source.path)
    path.unlink()

    replay = ImcodexArtifactMaterializer(
        artifact_stager=OutboundArtifactStager(tmp_path / "spool")
    )
    replayed = await replay.materialize_completed_item(facts)

    assert replayed is not None
    assert Path(replayed.attachments[0].source.path) == path
    assert path.read_bytes() == stream.getvalue()


@pytest.mark.asyncio
async def test_a1_candidate_overflow_releases_the_failed_staging_lease() -> None:
    stager = OverflowStager()
    materializer = ImcodexArtifactMaterializer(artifact_stager=stager)

    with pytest.raises(ValueError, match="only 4 outbound artifacts"):
        await materializer.materialize_completed_item(
            _item(
                kind=AppServerCompletedItemKind.DYNAMIC_TOOL_CALL,
                phase=AppServerCompletedItemPhase.NONE,
                candidates=(_candidate(),) * 5,
            )
        )

    assert stager.released == [
        "/spool/output-1.txt",
        "/spool/output-2.txt",
        "/spool/output-3.txt",
        "/spool/output-4.txt",
        "/spool/output-5.txt",
    ]
    assert await materializer.materialize_turn_terminal(_terminal()) is None


@pytest.mark.asyncio
async def test_live_activity_presenter_uses_only_typed_bounded_facts() -> None:
    presenter = ImcodexCodexLiveActivityPresenter()
    output = await presenter.present_live_activity(
        CodexLiveActivityFacts(
            event_id="plan-1",
            thread_ref=ThreadRef("codex-main", "thread-1"),
            turn_id="turn-1",
            kind=CodexLiveActivityKind.PLAN_UPDATED,
            native_method=CodexLiveActivityMethod.PLAN_UPDATED,
            summary="Implementation plan",
            plan=(CodexPlanStep(status="in_progress", step="Migrate composition"),),
        )
    )

    assert output is not None
    assert (
        output.content[0].text
        == "Implementation plan\n- [in_progress] Migrate composition"
    )


@pytest.mark.asyncio
async def test_outbound_visibility_is_applied_per_destination() -> None:
    store = FakeStore(show_commentary=False, show_toolcalls=True, show_system=False)
    presentation = ImcodexOutboundPresentation(store=store)
    live = OutboundPresentationContext(ProjectionPresentationOrigin.LIVE_ONLY)

    assert await presentation.present(_outbound("plan_updated"), live) is None
    assert await presentation.present(_outbound("command_execution"), live) is not None
    final = _outbound("agent_message", phase="final_answer")
    assert await presentation.present(final, live) is final


@pytest.mark.asyncio
async def test_pinned_sdk_recovery_metadata_keeps_final_and_artifact_fallback_visible() -> (
    None
):
    presentation = ImcodexOutboundPresentation(store=FakeStore(show_system=False))
    context = OutboundPresentationContext(ProjectionPresentationOrigin.AUTHORITATIVE)
    final = OutboundMessage(
        delivery_id="delivery-final",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(TextContent("Recovered answer"),),
        created_at=datetime.now(UTC),
        metadata={"native_application": "codex", "phase": "final_answer"},
    )
    artifact = OutboundMessage(
        delivery_id="delivery-artifact-fallback",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
        metadata={
            "native_application": "codex",
            "kind": "artifact_terminal_fallback",
        },
    )

    assert await presentation.present(final, context) is final
    assert await presentation.present(artifact, context) is artifact


@pytest.mark.asyncio
async def test_pinned_sdk_live_completed_metadata_obeys_commentary_visibility() -> None:
    presentation = ImcodexOutboundPresentation(store=FakeStore(show_commentary=False))
    message = OutboundMessage(
        delivery_id="delivery-live-commentary",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(TextContent("Live commentary"),),
        created_at=datetime.now(UTC),
        metadata={"native_method": "item/completed", "phase": "commentary"},
    )

    assert (
        await presentation.present(
            message,
            OutboundPresentationContext(ProjectionPresentationOrigin.LIVE_ONLY),
        )
        is None
    )


@pytest.mark.asyncio
async def test_a1_artifact_lease_is_tracked_or_released_by_o1() -> None:
    ledger = FakeArtifactLedger()
    visible = ImcodexOutboundPresentation(
        store=FakeStore(show_toolcalls=True), artifact_ledger=ledger
    )
    suppressed = ImcodexOutboundPresentation(
        store=FakeStore(show_toolcalls=False), artifact_ledger=ledger
    )
    message = OutboundMessage(
        delivery_id="delivery-artifact",
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(
            AttachmentContent(
                attachment_id="artifact-1",
                media_type="text/plain",
                source=LocalPath("/spool/result.txt"),
            ),
        ),
        created_at=datetime.now(UTC),
        metadata={
            "native_application": "codex",
            "native_item_kind": "command_execution",
        },
    )
    context = OutboundPresentationContext(ProjectionPresentationOrigin.LIVE_ONLY)

    assert await visible.present(message, context) is message
    assert await suppressed.present(message, context) is None
    assert ledger.tracked == [("delivery-artifact", ("/spool/result.txt",))]
    assert ledger.released == []


@pytest.mark.asyncio
async def test_webhook_visibility_reads_original_product_namespace() -> None:
    store = FakeStore(show_commentary=False)
    presentation = ImcodexOutboundPresentation(store=store)
    message = replace_conversation(
        _outbound("plan_updated"),
        ConversationRef(
            "webhook",
            encode_webhook_conversation("custom-a", "room/1"),
        ),
    )

    assert (
        await presentation.present(
            message,
            OutboundPresentationContext(ProjectionPresentationOrigin.LIVE_ONLY),
        )
        is None
    )
    assert store.binding_reads == [("custom-a", "room/1")]


def replace_conversation(
    message: OutboundMessage,
    conversation_ref: ConversationRef,
) -> OutboundMessage:
    return OutboundMessage(
        delivery_id=message.delivery_id,
        conversation_ref=conversation_ref,
        content=message.content,
        created_at=message.created_at,
        metadata=message.metadata,
    )

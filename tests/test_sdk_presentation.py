from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from imagent.applications import (
    AppServerArtifactCandidate,
    AppServerArtifactSourceKind,
    AppServerPresentationContext,
    AppServerPresentationItem,
)
from imagent.contracts import (
    AgentMessage,
    AttachmentContent,
    ConversationRef,
    MessageRole,
    OutboundMessage,
    TextContent,
    ThreadRef,
)

from imcodex.bridge.sdk_presentation import (
    ImcodexAppServerPresentation,
    ImcodexOutboundPresentation,
)
from imcodex.webhook_namespace import encode_webhook_conversation
from imcodex.models import OutboundArtifact


class FakeStore:
    def __init__(self, **visibility) -> None:
        self.binding_reads = []
        self.binding = SimpleNamespace(
            bootstrap_cwd="/repo",
            show_commentary=visibility.get("show_commentary", True),
            show_toolcalls=visibility.get("show_toolcalls", False),
            show_system=visibility.get("show_system", False),
        )

    def find_binding_by_thread_id(self, thread_id):
        del thread_id
        return self.binding

    def get_binding(self, channel_id, conversation_id):
        self.binding_reads.append((channel_id, conversation_id))
        return self.binding

    def get_thread_snapshot(self, thread_id):
        del thread_id
        return None


class FakeStager:
    def stage_appserver_candidate(self, candidate):
        return OutboundArtifact(
            kind="image",
            local_path="/spool/output.png",
            content_type="image/png",
            filename="output.png",
            size_bytes=3,
            sha256="abc123",
        )

    def stage_markdown_images(self, text, *, cwd):
        del text, cwd
        return ()


def _context(*, authoritative: bool = False) -> AppServerPresentationContext:
    return AppServerPresentationContext(
        ThreadRef("codex-main", "thread-1"),
        "turn-1",
        authoritative,
    )


def _item(**changes) -> AppServerPresentationItem:
    values = {
        "item_id": "answer-1",
        "item_kind": "agentmessage",
        "phase": "final_answer",
        "text": "Done",
        "command": "",
        "changed_paths": (),
        "artifact_candidates": (),
    }
    values.update(changes)
    return AppServerPresentationItem(**values)


def _message(*, kind: str = "agent_message") -> AgentMessage:
    return AgentMessage(
        agent_item_id="answer-1",
        thread_ref=ThreadRef("codex-main", "thread-1"),
        role=MessageRole.ASSISTANT,
        content=(TextContent("Done"),),
        created_at=datetime.now(UTC),
        metadata={"native_item_kind": kind, "phase": "final_answer"},
    )


def test_artifact_candidate_is_attached_to_the_next_final_answer() -> None:
    presentation = ImcodexAppServerPresentation(
        store=FakeStore(),
        artifact_stager=FakeStager(),
    )
    candidate = AppServerArtifactCandidate(
        candidate_id="tool-1:image:0",
        media_kind="image",
        source_kind=AppServerArtifactSourceKind.FILE_URL,
        value="file:///native/output.png",
    )

    assert (
        presentation.present_completed_item(
            _context(),
            _item(
                item_id="tool-1",
                item_kind="dynamictoolcall",
                phase="",
                text="",
                artifact_candidates=(candidate,),
            ),
            None,
        )
        is None
    )
    projected = presentation.present_completed_item(
        _context(),
        _item(),
        _message(),
    )

    assert projected is not None
    assert isinstance(projected.content[-1], AttachmentContent)
    assert projected.content[-1].attachment_id == "imcodex:artifact:abc123"
    assert presentation.present_turn_terminal(_context(), "completed") is None


def test_application_presentation_defers_visibility_and_keeps_terminal_fallback() -> None:
    presentation = ImcodexAppServerPresentation(
        store=FakeStore(show_commentary=False, show_toolcalls=False, show_system=False),
        artifact_stager=FakeStager(),
    )

    assert presentation.present_completed_item(
        _context(),
        _item(phase="commentary"),
        _message(),
    ) is not None
    assert presentation.present_live_message(_context(), _message(kind="plan_updated")) is not None
    presentation.observe_delta(_context(), "partial answer")

    fallback = presentation.present_turn_terminal(_context(), "interrupted")

    assert fallback is not None
    text = fallback.content[0]
    assert isinstance(text, TextContent)
    assert text.text == "Turn interrupted.\npartial answer"


@pytest.mark.asyncio
async def test_outbound_visibility_is_applied_per_destination() -> None:
    store = FakeStore(show_commentary=False, show_toolcalls=True, show_system=False)
    presentation = ImcodexOutboundPresentation(store=store)
    conversation = ConversationRef("telegram", "chat-1")

    def outbound(kind: str, *, phase: str = "") -> OutboundMessage:
        return OutboundMessage(
            delivery_id=f"delivery-{kind}-{phase}",
            conversation_ref=conversation,
            content=(TextContent("message"),),
            created_at=datetime.now(UTC),
            metadata={
                "native_application": "appserver",
                "native_item_kind": kind,
                "phase": phase,
            },
        )

    assert await presentation.present(outbound("plan_updated")) is None
    assert await presentation.present(outbound("command_execution")) is not None
    final = outbound("agent_message", phase="final_answer")
    assert await presentation.present(final) is final


@pytest.mark.asyncio
async def test_webhook_visibility_reads_original_product_namespace() -> None:
    store = FakeStore(show_commentary=False)
    presentation = ImcodexOutboundPresentation(store=store)
    message = OutboundMessage(
        delivery_id="delivery-webhook",
        conversation_ref=ConversationRef(
            "webhook",
            encode_webhook_conversation("custom-a", "room/1"),
        ),
        content=(TextContent("plan"),),
        created_at=datetime.now(UTC),
        metadata={
            "native_application": "appserver",
            "native_item_kind": "plan_updated",
        },
    )

    assert await presentation.present(message) is None
    assert store.binding_reads == [("custom-a", "room/1")]


def test_authoritative_and_live_buffers_do_not_cross_contaminate() -> None:
    presentation = ImcodexAppServerPresentation(
        store=FakeStore(),
        artifact_stager=FakeStager(),
    )
    presentation.observe_delta(_context(authoritative=False), "live")

    authoritative = presentation.present_turn_terminal(
        _context(authoritative=True),
        "completed",
    )
    live = presentation.present_turn_terminal(_context(authoritative=False), "completed")

    assert authoritative is None
    assert live is not None
    text = live.content[0]
    assert isinstance(text, TextContent)
    assert text.text == "live"


def test_terminal_delta_fallback_has_a_bounded_buffer() -> None:
    presentation = ImcodexAppServerPresentation(
        store=FakeStore(),
        artifact_stager=FakeStager(),
    )
    presentation.observe_delta(_context(), "x" * (70 * 1024))

    fallback = presentation.present_turn_terminal(_context(), "completed")

    assert fallback is not None
    text = fallback.content[0]
    assert isinstance(text, TextContent)
    assert len(text.text) < 65 * 1024
    assert text.text.endswith("[Output truncated while recovering the turn.]")

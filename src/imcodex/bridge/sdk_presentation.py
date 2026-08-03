from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from imagent.applications import (
    ApplicationArtifactMaterialization,
    ApplicationTextPresentation,
    AppServerCompletedItemFacts,
    AppServerCompletedItemPhase,
    AppServerTurnTerminalFacts,
    CodexLiveActivityFacts,
    CodexLiveActivityKind,
)
from imagent.contracts import (
    AttachmentContent,
    LocalPath,
    OutboundMessage,
    TextContent,
    TextFormat,
)
from imagent.outbound_presentation import (
    OutboundPresentationContext,
)

from ..models import OutboundArtifact
from ..webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    decode_webhook_conversation,
)

_MAX_OUTBOUND_ARTIFACTS = 4
_MAX_TURN_ARTIFACT_BUFFERS = 256


class ImcodexCodexLiveActivityPresenter:
    """Render only the bounded Codex live-activity facts accepted by A1."""

    async def present_live_activity(
        self,
        facts: CodexLiveActivityFacts,
    ) -> ApplicationTextPresentation | None:
        text = self._text(facts)
        if not text:
            return None
        return ApplicationTextPresentation((TextContent(text, TextFormat.MARKDOWN),))

    @staticmethod
    def _text(facts: CodexLiveActivityFacts) -> str:
        if facts.kind is CodexLiveActivityKind.PLAN_UPDATED:
            plan = "\n".join(f"- [{step.status}] {step.step}" for step in facts.plan)
            return "\n".join(
                part for part in (facts.summary or "Plan updated.", plan) if part
            )
        if facts.kind is CodexLiveActivityKind.DIFF_UPDATED:
            count = (
                f"{facts.changed_file_count} changed file(s)."
                if facts.changed_file_count is not None
                else ""
            )
            return "\n".join(
                part for part in (facts.summary or "Diff updated.", count) if part
            )
        if facts.kind is CodexLiveActivityKind.THREAD_STATUS_CHANGED:
            return f"Codex status: {facts.summary}" if facts.summary else ""
        if facts.kind is CodexLiveActivityKind.THREAD_COMPACTED:
            return facts.summary or "Codex compacted the Thread context."
        if facts.kind is CodexLiveActivityKind.MODEL_REROUTED:
            return facts.summary or "Codex rerouted the model."
        return ""


@dataclass(slots=True)
class _TurnArtifacts:
    artifacts: list[OutboundArtifact] = field(default_factory=list)


class ImcodexArtifactMaterializer:
    """Validate native candidates into the consumer-owned managed spool."""

    def __init__(self, *, artifact_stager) -> None:
        self.artifact_stager = artifact_stager
        self._lock = RLock()
        self._turns: OrderedDict[tuple[bool, str, str], _TurnArtifacts] = OrderedDict()

    async def materialize_completed_item(
        self,
        facts: AppServerCompletedItemFacts,
    ) -> ApplicationArtifactMaterialization | None:
        with self._lock:
            buffer = self._buffer(facts)
            original = list(buffer.artifacts)
            original_paths = {artifact.local_path for artifact in original}
            staged: list[OutboundArtifact] = []
            try:
                for candidate in facts.artifact_candidates:
                    artifact = self.artifact_stager.stage_appserver_candidate(candidate)
                    staged.append(artifact)
                    self._record_artifact(buffer, artifact)
            except BaseException:
                buffer.artifacts = original
                self.artifact_stager.release(
                    artifact
                    for artifact in staged
                    if artifact.local_path not in original_paths
                )
                raise
            if facts.phase is not AppServerCompletedItemPhase.FINAL_ANSWER:
                return None
            return self._take_materialization(buffer)

    async def materialize_turn_terminal(
        self,
        facts: AppServerTurnTerminalFacts,
    ) -> ApplicationArtifactMaterialization | None:
        with self._lock:
            buffer = self._turns.pop(self._key(facts), None)
            return self._take_materialization(buffer)

    def _buffer(self, facts: AppServerCompletedItemFacts) -> _TurnArtifacts:
        key = self._key(facts)
        buffer = self._turns.get(key)
        if buffer is not None:
            self._turns.move_to_end(key)
            return buffer
        if len(self._turns) >= _MAX_TURN_ARTIFACT_BUFFERS:
            raise RuntimeError("IMCodex artifact association capacity is exhausted")
        buffer = _TurnArtifacts()
        self._turns[key] = buffer
        return buffer

    @staticmethod
    def _key(
        facts: AppServerCompletedItemFacts | AppServerTurnTerminalFacts,
    ) -> tuple[bool, str, str]:
        return (
            facts.authoritative,
            facts.thread_ref.native_thread_id,
            facts.turn_id,
        )

    @staticmethod
    def _record_artifact(buffer: _TurnArtifacts, artifact: OutboundArtifact) -> None:
        identity = artifact.sha256 or Path(artifact.local_path).stem
        merged = {
            value.sha256 or Path(value.local_path).stem: value
            for value in buffer.artifacts
        }
        merged[identity] = artifact
        if len(merged) > _MAX_OUTBOUND_ARTIFACTS:
            raise ValueError(
                f"only {_MAX_OUTBOUND_ARTIFACTS} outbound artifacts can be delivered per turn"
            )
        buffer.artifacts = list(merged.values())

    @classmethod
    def _take_materialization(
        cls,
        buffer: _TurnArtifacts | None,
    ) -> ApplicationArtifactMaterialization | None:
        if buffer is None or not buffer.artifacts:
            return None
        attachments = tuple(cls._attachment(artifact) for artifact in buffer.artifacts)
        buffer.artifacts.clear()
        return ApplicationArtifactMaterialization(attachments)

    @staticmethod
    def _attachment(artifact: OutboundArtifact) -> AttachmentContent:
        identity = artifact.sha256 or Path(artifact.local_path).stem
        return AttachmentContent(
            attachment_id=f"imcodex:artifact:{identity}",
            media_type=artifact.content_type,
            source=LocalPath(artifact.local_path),
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
            metadata={"kind": artifact.kind, "sha256": artifact.sha256},
        )


class ImcodexOutboundPresentation:
    """Apply IMCodex visibility only after Gateway resolves a destination."""

    def __init__(self, *, store, artifact_ledger=None) -> None:
        self.store = store
        self.artifact_ledger = artifact_ledger

    async def present(
        self,
        message: OutboundMessage,
        context: OutboundPresentationContext,
    ) -> OutboundMessage | None:
        metadata = message.metadata
        channel_id, conversation_id = self._product_route(message)
        del context
        native_application = metadata.get("native_application")
        native_method = str(metadata.get("native_method") or "")
        if (
            native_application not in {"appserver", "codex", "zen"}
            and native_method != "item/completed"
        ):
            return self._track_artifacts(message)
        kind = str(metadata.get("native_item_kind") or metadata.get("kind") or "")
        phase = str(metadata.get("phase") or "")
        if phase == "final_answer" or kind in {
            "artifact_materialization",
            "artifact_terminal_fallback",
        }:
            return self._track_artifacts(message)
        binding = self.store.get_binding(channel_id, conversation_id)
        if phase == "commentary" or kind in {"agent_message", "plan_updated"}:
            visible = bool(getattr(binding, "show_commentary", True))
        elif kind in {"command_execution", "file_change", "diff_updated"}:
            visible = bool(getattr(binding, "show_toolcalls", False))
        else:
            visible = bool(getattr(binding, "show_system", False))
        if visible:
            return self._track_artifacts(message)
        return None

    def _track_artifacts(self, message: OutboundMessage) -> OutboundMessage:
        paths = self._artifact_paths(message)
        if self.artifact_ledger is not None and paths:
            self.artifact_ledger.track_attempt(message.delivery_id, paths)
        return message

    @staticmethod
    def _artifact_paths(message: OutboundMessage) -> tuple[str, ...]:
        return tuple(
            item.source.path
            for item in message.content
            if isinstance(item, AttachmentContent)
            and isinstance(item.source, LocalPath)
        )

    @staticmethod
    def _product_route(message: OutboundMessage) -> tuple[str, str]:
        conversation = message.conversation_ref
        if conversation.channel_instance_id == WEBHOOK_CHANNEL_INSTANCE_ID:
            return decode_webhook_conversation(conversation.native_conversation_id)
        return conversation.channel_instance_id, conversation.native_conversation_id

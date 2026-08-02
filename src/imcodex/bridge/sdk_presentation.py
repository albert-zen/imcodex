from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from imagent.applications import AppServerPresentationContext, AppServerPresentationItem
from imagent.contracts import AgentMessage, AttachmentContent, LocalPath, MessageRole, TextContent

from ..models import OutboundArtifact
from .message_pump import EMPTY_COMPLETED_TURN_TEXT


_MAX_OUTBOUND_ARTIFACTS = 4
_MAX_DELTA_CHARS = 64 * 1024
_MAX_COMMAND_SUMMARIES = 64
_MAX_CHANGED_FILES = 128
_MAX_ARTIFACT_ERRORS = 8


@dataclass(slots=True)
class _TurnPresentation:
    deltas: list[str] = field(default_factory=list)
    delta_chars: int = 0
    deltas_truncated: bool = False
    command_summaries: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    final_text: str = ""
    final_visible: bool = False
    artifacts: list[OutboundArtifact] = field(default_factory=list)
    artifact_errors: list[str] = field(default_factory=list)


class ImcodexAppServerPresentation:
    """IMCodex visibility and managed-spool policy over the SDK Application seam."""

    def __init__(self, *, store, artifact_stager) -> None:
        self.store = store
        self.artifact_stager = artifact_stager
        self._lock = RLock()
        self._turns: dict[tuple[bool, str, str], _TurnPresentation] = {}

    def present_completed_item(
        self,
        context: AppServerPresentationContext,
        item: AppServerPresentationItem,
        default_message: AgentMessage | None,
    ) -> AgentMessage | None:
        with self._lock:
            buffer = self._buffer(context)
            self._stage_candidates(buffer, item)
            if item.item_kind == "agentmessage":
                if item.phase == "final_answer" and item.text.strip():
                    self._stage_markdown_images(buffer, context, item.text)
                    buffer.final_text = item.text
                    buffer.final_visible = True
                elif not item.phase and item.text:
                    buffer.final_text = item.text
            elif item.item_kind == "commandexecution" and item.command:
                if len(buffer.command_summaries) < _MAX_COMMAND_SUMMARIES:
                    buffer.command_summaries.append(f"Executed `{item.command}`")
            elif item.item_kind == "filechange":
                remaining = _MAX_CHANGED_FILES - len(buffer.changed_files)
                if remaining > 0:
                    buffer.changed_files.extend(item.changed_paths[:remaining])

            if default_message is None or not self._item_visible(context, item):
                return None
            if item.item_kind != "agentmessage" or item.phase != "final_answer":
                return default_message
            return self._attach_buffer(default_message, buffer)

    def present_live_message(
        self,
        context: AppServerPresentationContext,
        message: AgentMessage,
    ) -> AgentMessage | None:
        kind = str(message.metadata.get("native_item_kind") or "")
        binding = self._binding(context)
        if kind == "plan_updated":
            return message if self._flag(binding, "show_commentary", True) else None
        if kind == "diff_updated":
            return message if self._flag(binding, "show_toolcalls", False) else None
        return message if self._flag(binding, "show_system", False) else None

    def observe_delta(self, context: AppServerPresentationContext, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            buffer = self._buffer(context)
            remaining = _MAX_DELTA_CHARS - buffer.delta_chars
            if remaining <= 0:
                buffer.deltas_truncated = True
                return
            accepted = delta[:remaining]
            buffer.deltas.append(accepted)
            buffer.delta_chars += len(accepted)
            buffer.deltas_truncated = len(accepted) != len(delta)

    def present_turn_terminal(
        self,
        context: AppServerPresentationContext,
        status: str,
    ) -> AgentMessage | None:
        with self._lock:
            buffer = self._turns.pop(self._key(context), None)
            if buffer is None or buffer.final_visible:
                return None
            text = self._terminal_text(buffer, status)
            message = AgentMessage(
                agent_item_id=f"{context.turn_id or 'turn'}:imcodex-terminal",
                thread_ref=context.thread_ref,
                role=MessageRole.ASSISTANT,
                content=(TextContent(text),),
                created_at=datetime.now(UTC),
                metadata={"status": status, "imcodex_terminal_fallback": True},
            )
            return self._attach_buffer(message, buffer)

    def _stage_candidates(
        self,
        buffer: _TurnPresentation,
        item: AppServerPresentationItem,
    ) -> None:
        for candidate in item.artifact_candidates:
            try:
                artifact = self.artifact_stager.stage_appserver_candidate(candidate)
            except (OSError, ValueError) as exc:
                self._record_error(buffer, str(exc))
                continue
            self._record_artifact(buffer, artifact)

    def _stage_markdown_images(
        self,
        buffer: _TurnPresentation,
        context: AppServerPresentationContext,
        text: str,
    ) -> None:
        try:
            artifacts = self.artifact_stager.stage_markdown_images(
                text,
                cwd=self._thread_cwd(context.thread_ref.native_thread_id),
            )
        except (OSError, ValueError) as exc:
            self._record_error(buffer, str(exc))
            return
        for artifact in artifacts:
            self._record_artifact(buffer, artifact)

    @staticmethod
    def _record_artifact(buffer: _TurnPresentation, artifact: OutboundArtifact) -> None:
        merged = {Path(value.local_path).stem: value for value in buffer.artifacts}
        merged[Path(artifact.local_path).stem] = artifact
        buffer.artifacts = list(merged.values())[:_MAX_OUTBOUND_ARTIFACTS]
        if len(merged) > _MAX_OUTBOUND_ARTIFACTS:
            ImcodexAppServerPresentation._record_error(
                buffer,
                f"only {_MAX_OUTBOUND_ARTIFACTS} outbound artifacts can be delivered per turn",
            )

    @staticmethod
    def _record_error(buffer: _TurnPresentation, error: str) -> None:
        if (
            error
            and error not in buffer.artifact_errors
            and len(buffer.artifact_errors) < _MAX_ARTIFACT_ERRORS
        ):
            buffer.artifact_errors.append(error)

    def _attach_buffer(
        self,
        message: AgentMessage,
        buffer: _TurnPresentation,
    ) -> AgentMessage:
        attachments = tuple(self._attachment(artifact) for artifact in buffer.artifacts)
        text_notice = self._artifact_error_notice(buffer)
        content = message.content
        if text_notice:
            content = (*content, TextContent(text_notice))
        buffer.artifacts.clear()
        buffer.artifact_errors.clear()
        return replace(message, content=(*content, *attachments))

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

    @staticmethod
    def _artifact_error_notice(buffer: _TurnPresentation) -> str:
        if not buffer.artifact_errors:
            return ""
        lines = "\n".join(f"- {error}" for error in buffer.artifact_errors)
        return f"Attachment delivery unavailable:\n{lines}"

    @staticmethod
    def _terminal_text(buffer: _TurnPresentation, status: str) -> str:
        normalized = status.strip().casefold()
        final_text = buffer.final_text or "".join(buffer.deltas)
        if not buffer.final_text and buffer.deltas_truncated:
            final_text = f"{final_text}\n[Output truncated while recovering the turn.]"
        changes = ""
        if buffer.changed_files:
            changes = "\n".join(
                ("Changed files:", *(f"- {path}" for path in dict.fromkeys(buffer.changed_files)))
            )
        if normalized == "completed":
            text = final_text
        elif normalized == "interrupted":
            text = "\n".join(part for part in ("Turn interrupted.", final_text, changes) if part)
        else:
            text = "\n".join(part for part in ("Turn failed.", final_text, changes) if part)
        if not text and buffer.command_summaries:
            text = "\n".join(buffer.command_summaries)
        return text or EMPTY_COMPLETED_TURN_TEXT

    def _item_visible(
        self,
        context: AppServerPresentationContext,
        item: AppServerPresentationItem,
    ) -> bool:
        binding = self._binding(context)
        if item.item_kind == "agentmessage":
            return item.phase == "final_answer" or self._flag(binding, "show_commentary", True)
        if item.item_kind in {"commandexecution", "filechange"}:
            return self._flag(binding, "show_toolcalls", False)
        return False

    def _binding(self, context: AppServerPresentationContext):
        return self.store.find_binding_by_thread_id(context.thread_ref.native_thread_id)

    @staticmethod
    def _flag(binding, name: str, default: bool) -> bool:
        return bool(getattr(binding, name, default))

    def _thread_cwd(self, thread_id: str) -> str:
        snapshot = self.store.get_thread_snapshot(thread_id)
        if snapshot is not None and snapshot.cwd:
            return snapshot.cwd
        binding = self.store.find_binding_by_thread_id(thread_id)
        return str(getattr(binding, "bootstrap_cwd", "") or "")

    def _buffer(self, context: AppServerPresentationContext) -> _TurnPresentation:
        return self._turns.setdefault(self._key(context), _TurnPresentation())

    @staticmethod
    def _key(context: AppServerPresentationContext) -> tuple[bool, str, str]:
        return (
            context.authoritative,
            context.thread_ref.native_thread_id,
            context.turn_id,
        )

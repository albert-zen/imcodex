from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import OutboundArtifact, OutboundMessage


EMPTY_COMPLETED_TURN_TEXT = "Codex completed the turn without returning a final message."
_MAX_OUTBOUND_ARTIFACTS = 4


@dataclass(slots=True)
class TurnBuffer:
    deltas: list[str] = field(default_factory=list)
    command_summaries: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    emitted_progress_texts: set[str] = field(default_factory=set)
    final_text: str = ""
    final_visible: bool = False
    artifacts: list[OutboundArtifact] = field(default_factory=list)
    artifact_errors: list[str] = field(default_factory=list)


class MessagePump:
    def __init__(self) -> None:
        self._turns: dict[tuple[str, str], TurnBuffer] = {}

    def record_delta(
        self,
        *,
        thread_id: str,
        turn_id: str,
        delta: str,
        emit_progress: bool,
    ) -> OutboundMessage | None:
        buffer = self._buffer(thread_id, turn_id)
        if delta:
            buffer.deltas.append(delta)
        if not emit_progress or buffer.final_visible or not delta:
            return None
        return self._emit_progress(buffer, delta)

    def record_agent_message(
        self,
        *,
        thread_id: str,
        turn_id: str,
        phase: str | None,
        text: str,
        emit_commentary: bool,
    ) -> OutboundMessage | None:
        buffer = self._buffer(thread_id, turn_id)
        if phase == "final_answer":
            if not text.strip():
                # Do not mark a blank item as visibly delivered. A later
                # turn/completed event can still fall back to accumulated
                # deltas or native recovery.
                return None
            buffer.final_text = text
            buffer.final_visible = True
            return OutboundMessage(
                channel_id="",
                conversation_id="",
                message_type="turn_result",
                text=self._with_artifact_errors(text, buffer),
                artifacts=tuple(buffer.artifacts),
            )
        if phase is None:
            if text:
                buffer.final_text = text
            if not text or not emit_commentary or buffer.final_visible:
                return None
            return self._emit_progress(buffer, text)
        if not text or not emit_commentary or buffer.final_visible:
            return None
        return self._emit_progress(buffer, text)

    def record_command(
        self,
        *,
        thread_id: str,
        turn_id: str,
        command: str,
        emit_progress: bool,
    ) -> OutboundMessage | None:
        buffer = self._buffer(thread_id, turn_id)
        text = f"Executed `{command}`"
        buffer.command_summaries.append(text)
        if not emit_progress or buffer.final_visible:
            return None
        return self._emit_progress(buffer, text)

    def record_file_change(
        self,
        *,
        thread_id: str,
        turn_id: str,
        paths: list[str],
        emit_progress: bool,
    ) -> OutboundMessage | None:
        buffer = self._buffer(thread_id, turn_id)
        buffer.changed_files.extend(paths)
        if not paths or not emit_progress or buffer.final_visible:
            return None
        lines = ["Changed files:"]
        lines.extend(f"- {path}" for path in paths)
        return self._emit_progress(buffer, "\n".join(lines))

    def record_artifacts(
        self,
        *,
        thread_id: str,
        turn_id: str,
        artifacts: tuple[OutboundArtifact, ...] = (),
        error: str | None = None,
    ) -> None:
        buffer = self._buffer(thread_id, turn_id)
        merged = {Path(artifact.local_path).stem: artifact for artifact in buffer.artifacts}
        merged.update({Path(artifact.local_path).stem: artifact for artifact in artifacts})
        values = list(merged.values())
        buffer.artifacts = values[:_MAX_OUTBOUND_ARTIFACTS]
        if len(values) > _MAX_OUTBOUND_ARTIFACTS:
            error = f"only {_MAX_OUTBOUND_ARTIFACTS} outbound artifacts can be delivered per turn"
        if error and error not in buffer.artifact_errors:
            buffer.artifact_errors.append(error)

    def finalize_turn(self, *, thread_id: str, turn_id: str, status: str) -> OutboundMessage | None:
        buffer = self._turns.pop((thread_id, turn_id), None)
        if buffer is None:
            if status == "completed":
                return OutboundMessage(
                    channel_id="",
                    conversation_id="",
                    message_type="turn_result",
                    text=EMPTY_COMPLETED_TURN_TEXT,
                )
            text = "Turn interrupted." if status == "interrupted" else "Turn failed."
            return OutboundMessage(channel_id="", conversation_id="", message_type="turn_result", text=text)
        if status == "completed" and buffer.final_visible:
            return None
        final_text = buffer.final_text or "".join(buffer.deltas)
        changed_files_text = ""
        if buffer.changed_files:
            lines = ["Changed files:"]
            lines.extend(f"- {path}" for path in dict.fromkeys(buffer.changed_files))
            changed_files_text = "\n".join(lines)
        if status == "completed":
            text = final_text
        elif status == "interrupted":
            text = "\n".join(part for part in ("Turn interrupted.", final_text, changed_files_text) if part)
        else:
            text = "\n".join(part for part in ("Turn failed.", final_text, changed_files_text) if part)
        if not text and buffer.command_summaries:
            text = "\n".join(buffer.command_summaries)
        if status == "completed" and not text.strip():
            text = EMPTY_COMPLETED_TURN_TEXT
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_result",
            text=self._with_artifact_errors(text, buffer),
            artifacts=tuple(buffer.artifacts),
        )

    def discard_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._turns.pop((thread_id, turn_id), None)

    def active_artifact_paths(self) -> set[str]:
        return {
            artifact.local_path
            for buffer in self._turns.values()
            for artifact in buffer.artifacts
        }

    def recover_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
        items: list[dict],
        artifacts: tuple[OutboundArtifact, ...] = (),
        artifact_errors: tuple[str, ...] = (),
    ) -> OutboundMessage | None:
        buffer = self._turns.pop((thread_id, turn_id), None)
        final_text = ""
        changed_files: list[str] = []
        for item in items:
            item_type = str(item.get("type") or "")
            if item_type == "agentMessage" and str(item.get("text") or "").strip():
                final_text = str(item.get("text") or "")
            elif item_type == "fileChange":
                changed_files.extend(
                    str(change.get("path"))
                    for change in item.get("changes", [])
                    if isinstance(change, dict) and change.get("path")
                )
        if not final_text and buffer is not None:
            final_text = buffer.final_text or "".join(buffer.deltas)
        if buffer is not None:
            changed_files.extend(buffer.changed_files)
            if not final_text and buffer.command_summaries:
                final_text = "\n".join(buffer.command_summaries)
        normalized_status = status.strip().lower()
        if normalized_status == "completed":
            text = final_text or EMPTY_COMPLETED_TURN_TEXT
        elif normalized_status == "interrupted":
            text = "\n".join(part for part in ("Turn interrupted.", final_text) if part)
        else:
            text = "\n".join(part for part in ("Turn failed.", final_text) if part)
        if changed_files:
            changes = "\n".join(
                ["Changed files:", *(f"- {path}" for path in dict.fromkeys(changed_files))]
            )
            text = "\n".join(part for part in (text, changes) if part)
        if artifact_errors:
            notice = "\n".join(f"- {error}" for error in artifact_errors)
            text = "\n\n".join(
                part for part in (text, f"Attachment delivery unavailable:\n{notice}") if part
            )
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_result",
            text=text,
            artifacts=artifacts,
        )

    def _buffer(self, thread_id: str, turn_id: str) -> TurnBuffer:
        key = (thread_id, turn_id)
        buffer = self._turns.get(key)
        if buffer is None:
            buffer = TurnBuffer()
            self._turns[key] = buffer
        return buffer

    def _emit_progress(self, buffer: TurnBuffer, text: str) -> OutboundMessage | None:
        if text in buffer.emitted_progress_texts:
            return None
        buffer.emitted_progress_texts.add(text)
        return OutboundMessage(channel_id="", conversation_id="", message_type="turn_progress", text=text)

    @staticmethod
    def _with_artifact_errors(text: str, buffer: TurnBuffer) -> str:
        if not buffer.artifact_errors:
            return text
        notice = "\n".join(f"- {error}" for error in buffer.artifact_errors)
        return "\n\n".join(
            part for part in (text, f"Attachment delivery unavailable:\n{notice}") if part
        )

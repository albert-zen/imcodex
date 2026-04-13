from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..models import OutboundMessage

ProgressKind = Literal["commentary", "toolcall"]


@dataclass(slots=True)
class TurnBuffer:
    deltas: list[str] = field(default_factory=list)
    command_summaries: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    emitted_progress_texts: set[str] = field(default_factory=set)
    final_text: str = ""
    final_visible: bool = False


class MessagePump:
    def __init__(
        self,
        *,
        progress_renderer=None,
        result_renderer=None,
    ) -> None:
        self.progress_renderer = progress_renderer or self._render_progress
        self.result_renderer = result_renderer or self._render_result
        self._turns: dict[tuple[str, str], TurnBuffer] = {}

    def record_delta(
        self,
        *,
        thread_id: str,
        turn_id: str,
        delta: str,
        emit_progress: bool,
    ) -> OutboundMessage | None:
        buffer = self._turn(thread_id, turn_id)
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
        buffer = self._turn(thread_id, turn_id)
        if phase is None:
            if text:
                buffer.final_text = text
            return None
        if phase == "final_answer":
            buffer.final_text = text
            buffer.final_visible = True
            return self.result_renderer(text, [], [], False, False)
        if not phase or phase == "final_answer" or not text or not emit_commentary or buffer.final_visible:
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
        buffer = self._turn(thread_id, turn_id)
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
        buffer = self._turn(thread_id, turn_id)
        buffer.changed_files.extend(paths)
        if not paths or not emit_progress or buffer.final_visible:
            return None
        lines = ["Changed files:"]
        lines.extend(f"- {path}" for path in paths)
        return self._emit_progress(buffer, "\n".join(lines))

    def finalize_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        buffer = self._turns.pop(key, None)
        if buffer is None:
            return None

        final_text = buffer.final_text or "".join(buffer.deltas)
        if status == "completed" and buffer.final_visible:
            return None
        return self._render_result(
            final_text,
            buffer.command_summaries,
            buffer.changed_files,
            status == "failed",
            status == "interrupted",
        )

    def discard_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._turns.pop((thread_id, turn_id), None)

    def _turn(self, thread_id: str, turn_id: str) -> TurnBuffer:
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
        return self.progress_renderer(text)

    def _render_progress(self, text: str) -> OutboundMessage:
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_progress",
            text=text,
        )

    def _render_result(
        self,
        final_text: str,
        command_summaries: list[str],
        changed_files: list[str],
        failed: bool,
        interrupted: bool,
    ) -> OutboundMessage:
        if not failed and not interrupted:
            lines = [final_text] if final_text else []
        else:
            status = "Turn interrupted." if interrupted else "Turn failed."
            lines = [status, final_text]
        if (failed or interrupted or not final_text) and command_summaries:
            lines.extend(command_summaries)
        if (failed or interrupted or not final_text) and changed_files:
            lines.append("Changed files:")
            lines.extend(f"- {path}" for path in changed_files)
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_result",
            text="\n".join(part for part in lines if part),
        )

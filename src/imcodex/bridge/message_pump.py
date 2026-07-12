from __future__ import annotations

from dataclasses import dataclass, field

from ..models import OutboundMessage


@dataclass(slots=True)
class TurnBuffer:
    deltas: list[str] = field(default_factory=list)
    command_summaries: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    emitted_progress_texts: set[str] = field(default_factory=set)
    final_text: str = ""
    final_visible: bool = False


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
            buffer.final_text = text
            buffer.final_visible = True
            return OutboundMessage(channel_id="", conversation_id="", message_type="turn_result", text=text)
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

    def finalize_turn(self, *, thread_id: str, turn_id: str, status: str) -> OutboundMessage | None:
        buffer = self._turns.pop((thread_id, turn_id), None)
        if buffer is None:
            if status == "completed":
                return None
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
        return OutboundMessage(channel_id="", conversation_id="", message_type="turn_result", text=text)

    def discard_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._turns.pop((thread_id, turn_id), None)

    def recover_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
        items: list[dict],
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
            text = final_text
        elif normalized_status == "interrupted":
            text = "\n".join(part for part in ("Turn interrupted.", final_text) if part)
        else:
            text = "\n".join(part for part in ("Turn failed.", final_text) if part)
        if changed_files:
            changes = "\n".join(
                ["Changed files:", *(f"- {path}" for path in dict.fromkeys(changed_files))]
            )
            text = "\n".join(part for part in (text, changes) if part)
        if not text:
            return None
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_result",
            text=text,
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

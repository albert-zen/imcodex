from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from ..models import OutboundMessage


class MessagePump:
    def __init__(
        self,
        *,
        progress_renderer: Callable[[str], OutboundMessage] | None = None,
        result_renderer: Callable[[str, list[str], list[str], bool, bool], OutboundMessage] | None = None,
    ) -> None:
        self.progress_renderer = progress_renderer or self._render_progress
        self.result_renderer = result_renderer or self._render_result
        self._turn_messages: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._turn_commands: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._turn_files: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._emitted_turn_results: set[tuple[str, str]] = set()

    def record_delta(
        self,
        *,
        thread_id: str,
        turn_id: str,
        delta: str,
        emit_progress: bool,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        self._turn_messages[key].append(delta)
        if delta and emit_progress:
            return self.progress_renderer(delta)
        return None

    def record_agent_message(
        self,
        *,
        thread_id: str,
        turn_id: str,
        phase: str | None,
        text: str,
        emit_commentary: bool,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        self._turn_messages[key] = [text]
        if phase == "final_answer":
            if key in self._emitted_turn_results:
                return None
            self._emitted_turn_results.add(key)
            return self.result_renderer(text, [], [], False, False)
        if phase and phase != "final_answer" and text and emit_commentary:
            return self.progress_renderer(text)
        return None

    def record_command(
        self,
        *,
        thread_id: str,
        turn_id: str,
        command: str,
        emit_progress: bool,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        text = f"Executed `{command}`"
        self._turn_commands[key].append(text)
        if emit_progress:
            return self.progress_renderer(text)
        return None

    def record_file_change(
        self,
        *,
        thread_id: str,
        turn_id: str,
        paths: list[str],
        emit_progress: bool,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        for path in paths:
            self._turn_files[key].append(path)
        if paths and emit_progress:
            lines = ["Changed files:"]
            lines.extend(f"- {path}" for path in paths)
            return self.progress_renderer("\n".join(lines))
        return None

    def finalize_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> OutboundMessage | None:
        key = (thread_id, turn_id)
        text = "\n".join(self._turn_messages.pop(key, []))
        commands = self._turn_commands.pop(key, [])
        files = self._turn_files.pop(key, [])
        if key in self._emitted_turn_results and status == "completed":
            self._emitted_turn_results.discard(key)
            return None
        if key in self._emitted_turn_results:
            self._emitted_turn_results.discard(key)
        return self.result_renderer(
            text,
            commands,
            files,
            status == "failed",
            status == "interrupted",
        )

    def discard_turn(self, *, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        self._turn_messages.pop(key, None)
        self._turn_commands.pop(key, None)
        self._turn_files.pop(key, None)
        self._emitted_turn_results.discard(key)

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

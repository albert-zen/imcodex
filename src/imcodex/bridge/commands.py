from __future__ import annotations

from dataclasses import dataclass
import os
import shlex

from ..store import ConversationStore


@dataclass(slots=True)
class ParsedCommand:
    name: str
    args: list[str]


@dataclass(slots=True)
class CommandResponse:
    action: str
    text: str
    project_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    ticket_id: str | None = None
    answers: dict[str, list[str]] | None = None


def parse_command(text: str) -> ParsedCommand:
    if not text.startswith("/"):
        raise ValueError("not a slash command")
    parts = [part.strip("\"'") for part in shlex.split(text[1:], posix=False)]
    if not parts:
        raise ValueError("empty slash command")
    return ParsedCommand(name=parts[0], args=parts[1:])


class CommandRouter:
    def __init__(self, store: ConversationStore):
        self.store = store

    def handle(self, channel_id: str, conversation_id: str, text: str) -> CommandResponse:
        command = parse_command(text)
        handler = getattr(self, f"_handle_{command.name.replace('-', '_')}", None)
        if handler is None:
            return CommandResponse(action="unknown", text=f"Unknown command: /{command.name}")
        return handler(channel_id, conversation_id, command.args)

    def _handle_projects(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        projects = self.store.list_projects()
        lines = ["Projects:"]
        for project in projects:
            lines.append(f"- {project.project_id} | {project.display_name} | {project.cwd}")
        return CommandResponse(action="projects.list", text="\n".join(lines))

    def _handle_project(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 2 or args[0] != "use":
            return CommandResponse(action="project.invalid", text="Usage: /project use <project-id>")
        project_id = args[1]
        self.store.set_active_project(channel_id, conversation_id, project_id)
        return CommandResponse(
            action="project.use",
            text=f"Switched to project {project_id}.",
            project_id=project_id,
        )

    def _handle_cwd(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return CommandResponse(action="project.cwd.invalid", text="Usage: /cwd <path>")
        resolved = os.path.abspath(os.path.expanduser(args[0]))
        if not os.path.isdir(resolved):
            return CommandResponse(action="project.cwd.missing", text=f"Directory not found: {resolved}")
        project = self.store.ensure_project(resolved)
        self.store.set_active_project(channel_id, conversation_id, project.project_id)
        return CommandResponse(
            action="project.cwd",
            text=f"Switched to project {project.project_id} at {project.cwd}.",
            project_id=project.project_id,
        )

    def _handle_threads(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_project_id is None and "--all" not in args:
            return CommandResponse(
                action="threads.missing_project",
                text="Please choose a project first with /projects and /project use <project-id>.",
            )
        project_id = None if "--all" in args else binding.active_project_id
        threads = self.store.list_threads(project_id)
        lines = ["Threads:"]
        for thread in threads:
            marker = "*" if thread.thread_id == binding.active_thread_id else "-"
            lines.append(f"{marker} {thread.thread_id} | {thread.preview} | {thread.cwd}")
        return CommandResponse(action="threads.list", text="\n".join(lines))

    def _handle_thread(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 2 or args[0] != "use":
            return CommandResponse(action="thread.invalid", text="Usage: /thread use <thread-id>")
        thread_id = args[1]
        thread = self.store.get_thread(thread_id)
        self.store.set_active_thread(channel_id, conversation_id, thread_id)
        return CommandResponse(
            action="thread.use",
            text=f"Switched to thread {thread_id} in project {thread.project_id}.",
            thread_id=thread_id,
            project_id=thread.project_id,
        )

    def _handle_new(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        return CommandResponse(action="thread.new", text="Creating a new thread.")

    def _handle_status(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        project_text = binding.active_project_id or "(none)"
        cwd_text = "(none)"
        if binding.active_project_id is not None:
            cwd_text = self.store.get_project(binding.active_project_id).cwd
        thread_text = binding.active_thread_id or "(none)"
        turn_text = binding.active_turn_id or "(none)"
        turn_status = binding.active_turn_status or "(idle)"
        text = (
            f"project={project_text}\n"
            f"cwd={cwd_text}\n"
            f"thread={thread_text}\n"
            f"turn={turn_text}\n"
            f"turn_status={turn_status}\n"
            f"pending_requests={len(binding.pending_request_ids)}"
        )
        return CommandResponse(action="status", text=text)

    def _handle_stop(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_turn_id is None:
            return CommandResponse(action="turn.stop.none", text="No active turn to stop.")
        return CommandResponse(
            action="turn.stop",
            text=f"Stopping turn {binding.active_turn_id}.",
            turn_id=binding.active_turn_id,
        )

    def _handle_approve(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "accept", "approval.accept")

    def _handle_approve_session(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(
            channel_id,
            conversation_id,
            args,
            "acceptForSession",
            "approval.accept_session",
        )

    def _handle_deny(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "decline", "approval.deny")

    def _handle_cancel(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "cancel", "approval.cancel")

    def _handle_answer(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) < 2:
            return CommandResponse(action="request.answer.invalid", text="Usage: /answer <ticket> <question=value>...")
        ticket_id = args[0]
        answers = self._parse_answers(args[1:])
        request = self.store.get_pending_request(ticket_id)
        if request is None:
            return CommandResponse(action="request.answer.missing", text=f"Unknown ticket: {ticket_id}")
        return CommandResponse(
            action="request.answer",
            text=f"Recorded answer for {ticket_id}.",
            ticket_id=ticket_id,
            answers=answers,
        )

    def _handle_resolution(
        self,
        channel_id: str,
        conversation_id: str,
        args: list[str],
        decision: str,
        action: str,
    ) -> CommandResponse:
        del channel_id, conversation_id
        if len(args) != 1:
            return CommandResponse(action=f"{action}.invalid", text="Usage: /<command> <ticket>")
        ticket_id = args[0]
        request = self.store.get_pending_request(ticket_id)
        if request is None:
            return CommandResponse(action=f"{action}.missing", text=f"Unknown ticket: {ticket_id}")
        return CommandResponse(action=action, text=f"Recorded {decision} for {ticket_id}.", ticket_id=ticket_id)

    def _parse_answers(self, pairs: list[str]) -> dict[str, list[str]]:
        answers: dict[str, list[str]] = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            answers[key] = [part for part in value.split(",") if part]
        return answers

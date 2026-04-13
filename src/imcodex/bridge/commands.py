from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
from collections.abc import Callable

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
    ticket_ids: list[str] | None = None
    missing_ticket_ids: list[str] | None = None
    answers: dict[str, list[str]] | None = None
    include_all: bool = False


def parse_command(text: str) -> ParsedCommand:
    if not text.startswith("/"):
        raise ValueError("not a slash command")
    parts = [part.strip("\"'") for part in shlex.split(text[1:], posix=False)]
    if not parts:
        raise ValueError("empty slash command")
    return ParsedCommand(name=parts[0], args=parts[1:])


class CommandRouter:
    def __init__(
        self,
        store: ConversationStore,
        diagnostics_provider: Callable[[], dict[str, object]] | None = None,
    ):
        self.store = store
        self.diagnostics_provider = diagnostics_provider or (lambda: {"pid": os.getpid()})

    def handle(self, channel_id: str, conversation_id: str, text: str) -> CommandResponse:
        command = parse_command(text)
        handler = getattr(self, f"_handle_{command.name.replace('-', '_')}", None)
        if handler is None:
            return CommandResponse(action="unknown", text=f"Unknown command: /{command.name}")
        return handler(channel_id, conversation_id, command.args)

    def _handle_projects(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        projects = self.store.list_projects()
        lines = ["Legacy project aliases:"]
        for project in projects:
            lines.append(f"- {project.cwd} (legacy id: {project.project_id})")
        return CommandResponse(action="projects.list", text="\n".join(lines))

    def _handle_project(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 2 or args[0] != "use":
            return CommandResponse(action="project.invalid", text="Usage: /project use <project-id>")
        project_id = args[1]
        project = self.store.get_project(project_id)
        self.store.set_active_project(channel_id, conversation_id, project_id)
        return CommandResponse(
            action="project.use",
            text=f"CWD set to {project.cwd}. Prefer /cwd <path> for future switches.",
            project_id=project_id,
        )

    def _handle_cwd(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return CommandResponse(action="project.cwd.invalid", text="Usage: /cwd <path>")
        resolved = os.path.abspath(os.path.expanduser(args[0]))
        if not os.path.isdir(resolved):
            return CommandResponse(action="project.cwd.missing", text=f"Directory not found: {resolved}")
        binding = self.store.set_selected_cwd(channel_id, conversation_id, resolved)
        return CommandResponse(
            action="project.cwd",
            text=f"CWD set to {binding.selected_cwd}.",
            project_id=binding.active_project_id,
        )

    def _handle_threads(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        include_all = "--all" in args
        if binding.selected_cwd is None and not include_all:
            return CommandResponse(
                action="threads.missing_project",
                text="Choose a CWD first with /cwd <path>.",
            )
        return CommandResponse(action="threads.query", text="", include_all=include_all)

    def _handle_thread(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) == 1 and args[0] == "read":
            return self._handle_thread_read(channel_id, conversation_id)
        if len(args) != 2 or args[0] not in {"use", "attach"}:
            return CommandResponse(action="thread.invalid", text="Usage: /thread use <thread-id>, /thread attach <thread-id>, or /thread read")
        if args[0] == "attach":
            thread_id = args[1]
            binding = self.store.get_binding(channel_id, conversation_id)
            if binding.selected_cwd is None:
                text = f"Attaching thread {thread_id}."
            else:
                text = f"Attaching thread {thread_id} from CWD {binding.selected_cwd}."
            return CommandResponse(
                action="thread.attach",
                text=text,
                thread_id=thread_id,
                project_id=binding.active_project_id,
            )
        thread_id = args[1]
        thread = self.store.get_thread(thread_id)
        self.store.set_active_thread(channel_id, conversation_id, thread_id)
        return CommandResponse(
            action="thread.use",
            text=f"Switched to thread {self._thread_label(thread_id)} (id: {thread_id}) in CWD {thread.cwd}.",
            thread_id=thread_id,
            project_id=thread.project_id,
        )

    def _handle_thread_read(self, channel_id: str, conversation_id: str) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_thread_id is None:
            return CommandResponse(action="thread.read.none", text="No active thread.")
        return CommandResponse(
            action="thread.read.query",
            text="",
            thread_id=binding.active_thread_id,
        )

    def _handle_new(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.selected_cwd is None:
            return CommandResponse(
                action="thread.new.missing_project",
                text="Choose a CWD first with /cwd <path>.",
            )
        return CommandResponse(action="thread.new", text=f"Starting a thread in {binding.selected_cwd}.")

    def _handle_status(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        cwd_text = binding.selected_cwd or "(none)"
        thread_text = "(none)"
        thread_path_text = "(none)"
        thread_status_text = "(none)"
        if binding.active_thread_id is not None:
            thread_text = self._thread_label(binding.active_thread_id, binding=binding)
            thread_path_text = binding.last_seen_thread_path or "(unknown)"
            thread_status_text = (
                self._humanize_status(binding.last_seen_thread_status)
                if binding.last_seen_thread_status
                else "(unknown)"
            )
        turn_text = binding.active_turn_id or "(none)"
        turn_status = self._humanize_status(binding.active_turn_status) if binding.active_turn_status else "idle"
        model_text = binding.selected_model or "(default)"
        lines = [
            f"CWD: {cwd_text}",
            f"Thread: {thread_text}",
            f"Thread ID: {binding.active_thread_id or '(none)'}",
            f"Thread Path: {thread_path_text}",
            f"Thread Status: {thread_status_text}",
            f"Turn: {turn_text}",
            f"Turn Status: {turn_status}",
            f"Model: {model_text}",
            f"Permission Profile: {binding.permission_profile}",
            f"Visibility: {binding.visibility_profile}",
            f"Commentary: {'shown' if binding.show_commentary else 'hidden'}",
            f"Tool Calls: {'shown' if binding.show_toolcalls else 'hidden'}",
            f"Tickets: {len(binding.pending_request_ids)} pending",
        ]
        text = "\n".join(lines)
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

    def _handle_recover(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.active_thread_id is None:
            return CommandResponse(action="recover.none", text="No active thread to recover.")
        thread_id = binding.active_thread_id
        self.store.clear_active_thread(channel_id, conversation_id)
        return CommandResponse(
            action="recover",
            text=(
                f"Cleared stale thread binding {thread_id}. "
                "Use /new, /thread attach <thread-id>, or send a new prompt to start a fresh thread."
            ),
            thread_id=thread_id,
        )

    def _handle_requests(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        requests = self.store.list_pending_requests(channel_id, conversation_id)
        if not requests:
            return CommandResponse(action="requests.list", text="No pending requests.")
        lines = ["Pending requests:"]
        for request in requests:
            lines.append(f"[{request.ticket_id}] {request.kind}: {request.summary}")
        return CommandResponse(action="requests.list", text="\n".join(lines))

    def _handle_doctor(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        info = self.diagnostics_provider()
        lines = [
            f"Codex binary: {info.get('codex_bin', '(unknown)')}",
            f"App Server: {info.get('app_server', '(unknown)')}",
            f"Bridge: {info.get('bridge', '(unknown)')}",
            f"PID: {info.get('pid', '(unknown)')}",
            f"Data dir: {info.get('data_dir', '(unknown)')}",
            f"Permission Profile: {binding.permission_profile}",
            f"Model: {binding.selected_model or '(default)'}",
            f"Visibility: {binding.visibility_profile}",
            f"Commentary: {'shown' if binding.show_commentary else 'hidden'}",
            f"Tool Calls: {'shown' if binding.show_toolcalls else 'hidden'}",
        ]
        return CommandResponse(action="doctor", text="\n".join(lines))

    def _handle_permissions(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1 or args[0] not in {"autonomous", "review"}:
            return CommandResponse(
                action="settings.permissions.invalid",
                text="Usage: /permissions autonomous|review",
            )
        profile = args[0]
        self.store.set_permission_profile(channel_id, conversation_id, profile)
        return CommandResponse(
            action="settings.permissions",
            text=f"Permission profile set to {profile}.",
        )

    def _handle_view(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1 or args[0] not in {"minimal", "standard", "verbose"}:
            return CommandResponse(
                action="settings.view.invalid",
                text="Usage: /view minimal|standard|verbose",
            )
        profile = args[0]
        self.store.set_visibility_profile(channel_id, conversation_id, profile)
        return CommandResponse(
            action="settings.view",
            text=f"Visibility profile set to {profile}.",
        )

    def _handle_model(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return CommandResponse(
                action="settings.model.invalid",
                text="Usage: /model <name|default>",
            )
        if args[0] == "default":
            self.store.set_model_override(channel_id, conversation_id, None)
            return CommandResponse(
                action="settings.model",
                text="Model override cleared; using the default Codex model.",
            )
        self.store.set_model_override(channel_id, conversation_id, args[0])
        return CommandResponse(
            action="settings.model",
            text=f"Model override set to {args[0]}.",
        )

    def _handle_help(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        lines = [
            "Commands:",
            "/cwd <path>",
            "/threads [--all]",
            "/thread attach <thread-id>",
            "/thread read",
            "/new",
            "/recover",
            "/status",
            "/stop",
            "/requests",
            "/approve <ticket...>",
            "/approve-session <ticket...>",
            "/deny <ticket...>",
            "/cancel <ticket...>",
            "/answer <ticket> key=value ...",
            "/permissions autonomous",
            "/permissions review",
            "/model <name|default>",
            "/view minimal|standard|verbose",
            "/show commentary|toolcalls",
            "/hide commentary|toolcalls",
            "/doctor",
            "",
            "Legacy compatibility:",
            "/projects (legacy alias)",
            "/project use <project-id> (legacy alias)",
        ]
        return CommandResponse(action="help", text="\n".join(lines))

    def _handle_show(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, args, enabled=True)

    def _handle_hide(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, args, enabled=False)

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
        request = self.store.get_pending_request(
            ticket_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if request is None:
            return CommandResponse(action="request.answer.missing", text=f"Unknown ticket: {ticket_id}")
        if request.kind != "question":
            return CommandResponse(
                action="request.answer.invalid_kind",
                text=f"Ticket {ticket_id} is not a question request.",
                ticket_id=ticket_id,
            )
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
        if len(args) < 1:
            return CommandResponse(action=f"{action}.invalid", text="Usage: /<command> <ticket> [ticket...]")
        ticket_ids: list[str] = []
        missing_ticket_ids: list[str] = []
        for ticket_id in args:
            request = self.store.get_pending_request(
                ticket_id,
                channel_id=channel_id,
                conversation_id=conversation_id,
            )
            if request is None or request.kind != "approval":
                missing_ticket_ids.append(ticket_id)
            else:
                ticket_ids.append(ticket_id)
        if not ticket_ids:
            return CommandResponse(
                action=f"{action}.missing",
                text=f"Unknown tickets: {', '.join(missing_ticket_ids)}.",
                missing_ticket_ids=missing_ticket_ids,
            )
        parts = [f"Recorded {decision} for {', '.join(ticket_ids)}."]
        if missing_ticket_ids:
            parts.append(f"Unknown tickets: {', '.join(missing_ticket_ids)}.")
        return CommandResponse(
            action=action,
            text=" ".join(parts),
            ticket_id=ticket_ids[0] if len(ticket_ids) == 1 else None,
            ticket_ids=ticket_ids,
            missing_ticket_ids=missing_ticket_ids or None,
        )

    def _handle_visibility_toggle(
        self,
        channel_id: str,
        conversation_id: str,
        args: list[str],
        *,
        enabled: bool,
    ) -> CommandResponse:
        if len(args) != 1 or args[0] not in {"commentary", "toolcalls"}:
            return CommandResponse(
                action="settings.visibility.invalid",
                text="Usage: /show|/hide commentary|toolcalls",
            )
        target = args[0]
        if target == "commentary":
            self.store.set_commentary_visibility(
                channel_id,
                conversation_id,
                enabled=enabled,
            )
            text = f"Commentary messages {'shown' if enabled else 'hidden'}."
        else:
            self.store.set_toolcall_visibility(
                channel_id,
                conversation_id,
                enabled=enabled,
            )
            text = f"Tool-call messages {'shown' if enabled else 'hidden'}."
        return CommandResponse(action="settings.visibility", text=text)

    def _thread_label(self, thread_id: str, *, binding=None) -> str:
        try:
            return self.store.thread_label(thread_id)
        except KeyError:
            if binding is not None and binding.active_thread_id == thread_id and binding.last_seen_thread_name:
                return binding.last_seen_thread_name
            return "Untitled thread"

    def _parse_answers(self, pairs: list[str]) -> dict[str, list[str]]:
        answers: dict[str, list[str]] = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            answers[key] = [part for part in value.split(",") if part]
        return answers

    def _humanize_status(self, status: str) -> str:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", status)
        return spaced.replace("_", " ").strip().lower()

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

from ..store import ConversationStore


@dataclass(slots=True)
class ParsedCommand:
    name: str
    args: list[str]


@dataclass(slots=True)
class CommandResponse:
    action: str
    text: str
    thread_id: str | None = None
    request_id: str | None = None
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
    def __init__(self, store: ConversationStore) -> None:
        self.store = store

    def handle(self, channel_id: str, conversation_id: str, text: str) -> CommandResponse:
        command = parse_command(text)
        handler = getattr(self, f"_handle_{command.name.replace('-', '_')}", None)
        if handler is None:
            return CommandResponse(action="unknown", text=f"Unknown command: /{command.name}")
        return handler(channel_id, conversation_id, command.args)

    def _handle_cwd(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return CommandResponse(action="project.cwd.invalid", text="Usage: /cwd <path>")
        resolved = os.path.abspath(os.path.expanduser(args[0]))
        if not os.path.isdir(resolved):
            return CommandResponse(action="project.cwd.missing", text=f"Directory not found: {resolved}")
        self.store.set_bootstrap_cwd(channel_id, conversation_id, resolved)
        return CommandResponse(action="project.cwd", text=f"CWD set to {resolved}.")

    def _handle_threads(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        include_all = "--all" in args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None and not include_all:
            return CommandResponse(action="threads.missing_project", text="Choose a CWD first with /cwd <path>.")
        return CommandResponse(action="threads.query", text="", include_all=include_all)

    def _handle_thread(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        if len(args) == 1 and args[0] == "read":
            if binding.thread_id is None:
                return CommandResponse(action="thread.read.none", text="No active thread.")
            return CommandResponse(action="thread.read.query", text="", thread_id=binding.thread_id)
        if len(args) == 2 and args[0] == "attach":
            return CommandResponse(action="thread.attach", text=f"Attaching thread {args[1]}.", thread_id=args[1])
        return CommandResponse(
            action="thread.invalid",
            text="Usage: /thread attach <thread-id> or /thread read",
        )

    def _handle_new(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        cwd = self.store.current_cwd(channel_id, conversation_id)
        if cwd is None:
            return CommandResponse(action="thread.new.missing_project", text="Choose a CWD first with /cwd <path>.")
        return CommandResponse(action="thread.new", text=f"Starting a thread in {cwd}.")

    def _handle_status(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        return CommandResponse(action="status.query", text="")

    def _handle_stop(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return CommandResponse(action="turn.stop.none", text="No active turn to stop.")
        active = self.store.get_active_turn(binding.thread_id)
        if active is None:
            return CommandResponse(action="turn.stop.none", text="No active turn to stop.")
        return CommandResponse(action="turn.stop", text=f"Stopping turn {active[0]}.")

    def _handle_requests(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del args
        requests = self.store.list_pending_requests(channel_id, conversation_id)
        if not requests:
            return CommandResponse(action="requests.list", text="No pending requests.")
        lines = ["Pending requests:"]
        for route in requests:
            handle = route.request_handle or route.request_id[:8]
            lines.append(f"- [{handle}] {route.kind}: {route.request_id}")
        return CommandResponse(action="requests.list", text="\n".join(lines))

    def _handle_approve(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "approval.accept", "accept")

    def _handle_deny(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "approval.deny", "decline")

    def _handle_cancel(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, args, "approval.cancel", "cancel")

    def _handle_answer(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if not args:
            return CommandResponse(action="request.answer.invalid", text="Usage: /answer <request-id> key=value ...")
        if "=" in args[0]:
            token = None
            answer_parts = args
        else:
            token = args[0]
            answer_parts = args[1:]
        if not answer_parts:
            return CommandResponse(action="request.answer.invalid", text="Usage: /answer <request-id> key=value ...")
        try:
            route = self.store.match_pending_request(channel_id, conversation_id, token, kind="question")
        except ValueError as exc:
            return CommandResponse(action="request.answer.missing", text=str(exc))
        if route is None:
            return CommandResponse(action="request.answer.missing", text="Unknown question request.")
        return CommandResponse(
            action="request.answer",
            text=f"Recorded answer for {route.request_id}.",
            request_id=route.request_id,
            answers=self._parse_answers(answer_parts),
        )

    def _handle_view(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1 or args[0] not in {"minimal", "standard", "verbose"}:
            return CommandResponse(action="settings.view.invalid", text="Usage: /view minimal|standard|verbose")
        self.store.set_visibility_profile(channel_id, conversation_id, args[0])
        return CommandResponse(action="settings.view", text=f"Visibility profile set to {args[0]}.")

    def _handle_show(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, args, enabled=True)

    def _handle_hide(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, args, enabled=False)

    def _handle_model(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return CommandResponse(action="settings.model.invalid", text="Usage: /model <name|default>")
        if args[0] == "default":
            self.store.set_next_model_override(channel_id, conversation_id, None)
            return CommandResponse(action="settings.model", text="Next-turn model override cleared.")
        self.store.set_next_model_override(channel_id, conversation_id, args[0])
        return CommandResponse(action="settings.model", text=f"Next turn will use model {args[0]}.")

    def _handle_help(self, channel_id: str, conversation_id: str, args: list[str]) -> CommandResponse:
        del channel_id, conversation_id, args
        return CommandResponse(
            action="help",
            text="\n".join(
                [
                    "Commands:",
                    "/cwd <path>",
                    "/threads [--all]",
                    "/thread attach <thread-id>",
                    "/thread read",
                    "/new",
                    "/status",
                    "/stop",
                    "/requests",
                    "/approve [request-id-or-prefix]",
                    "/deny [request-id-or-prefix]",
                    "/cancel [request-id-or-prefix]",
                    "/answer [request-id-or-prefix] key=value ...",
                    "/model <name|default>",
                    "/view minimal|standard|verbose",
                    "/show commentary|toolcalls",
                    "/hide commentary|toolcalls",
                ]
            ),
        )

    def _handle_resolution(
        self,
        channel_id: str,
        conversation_id: str,
        args: list[str],
        action: str,
        decision: str,
    ) -> CommandResponse:
        token = args[0] if args else None
        try:
            route = self.store.match_pending_request(channel_id, conversation_id, token, kind="approval")
        except ValueError as exc:
            return CommandResponse(action=f"{action}.missing", text=str(exc))
        if route is None:
            return CommandResponse(action=f"{action}.missing", text="Unknown approval request.")
        return CommandResponse(
            action=action,
            text=f"Recorded {decision} for {route.request_id}.",
            request_id=route.request_id,
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
            return CommandResponse(action="settings.visibility.invalid", text="Usage: /show|/hide commentary|toolcalls")
        if args[0] == "commentary":
            self.store.set_commentary_visibility(channel_id, conversation_id, enabled=enabled)
            return CommandResponse(action="settings.visibility", text=f"Commentary messages {'shown' if enabled else 'hidden'}.")
        self.store.set_toolcall_visibility(channel_id, conversation_id, enabled=enabled)
        return CommandResponse(action="settings.visibility", text=f"Tool-call messages {'shown' if enabled else 'hidden'}.")

    def _parse_answers(self, pairs: list[str]) -> dict[str, list[str]]:
        answers: dict[str, list[str]] = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            answers[key] = [part for part in value.split(",") if part]
        return answers

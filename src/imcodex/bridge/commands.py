from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..store import ConversationStore


_PERMISSION_PRESETS = {
    "default": [
        {"key_path": "approval_policy", "value": "on-request", "merge_strategy": "replace"},
        {"key_path": "sandbox_mode", "value": "workspace-write", "merge_strategy": "replace"},
    ],
    "read-only": [
        {"key_path": "approval_policy", "value": "on-request", "merge_strategy": "replace"},
        {"key_path": "sandbox_mode", "value": "read-only", "merge_strategy": "replace"},
    ],
    "full-access": [
        {"key_path": "approval_policy", "value": "never", "merge_strategy": "replace"},
        {"key_path": "sandbox_mode", "value": "danger-full-access", "merge_strategy": "replace"},
    ],
}
_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


@dataclass(slots=True)
class ParsedCommand:
    name: str
    args: list[str]
    raw_args_text: str = ""


@dataclass(slots=True)
class CommandResponse:
    action: str
    text: str
    thread_id: str | None = None
    request_id: str | None = None
    request_ids: list[str] | None = None
    answers: dict[str, list[str]] | None = None
    include_all: bool = False
    payload: dict | None = None


def parse_command(text: str) -> ParsedCommand:
    if not text.startswith("/"):
        raise ValueError("not a slash command")
    body = text[1:].strip()
    if not body:
        raise ValueError("empty slash command")
    name, _, raw_args_text = body.partition(" ")
    parts = [part.strip("\"'") for part in shlex.split(body, posix=False)]
    if not parts:
        raise ValueError("empty slash command")
    return ParsedCommand(name=name, args=parts[1:], raw_args_text=raw_args_text.strip())


class CommandRouter:
    def __init__(self, store: ConversationStore, playground_path: str | Path | None = None) -> None:
        self.store = store
        self.playground_path = Path(playground_path) if playground_path is not None else self._default_playground_path()

    def handle(self, channel_id: str, conversation_id: str, text: str) -> CommandResponse:
        command = parse_command(text)
        handler = getattr(self, f"_handle_{command.name.replace('-', '_')}", None)
        if handler is None:
            return CommandResponse(action="unknown", text=f"Unknown command: /{command.name}")
        return handler(channel_id, conversation_id, command)

    def _handle_cwd(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if not command.args:
            current = self.store.current_cwd(channel_id, conversation_id)
            if current is None:
                text = "No CWD selected. Use /cwd <path> or /cwd playground."
            else:
                text = f"Current CWD: {current}"
            return CommandResponse(action="project.cwd.read", text=text)
        if len(command.args) != 1:
            return CommandResponse(action="project.cwd.invalid", text="Usage: /cwd <path>")
        if command.args[0].lower() == "playground":
            resolved = self.playground_path
            resolved.mkdir(parents=True, exist_ok=True)
            self.store.set_bootstrap_cwd(channel_id, conversation_id, str(resolved))
            return CommandResponse(action="project.cwd", text=f"CWD set to {resolved}.")
        resolved = os.path.abspath(os.path.expanduser(command.args[0]))
        if not os.path.isdir(resolved):
            return CommandResponse(action="project.cwd.missing", text=f"Directory not found: {resolved}")
        self.store.set_bootstrap_cwd(channel_id, conversation_id, resolved)
        return CommandResponse(action="project.cwd", text=f"CWD set to {resolved}.")

    def _handle_threads(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        include_all = False
        page = 1
        query_parts: list[str] = []
        index = 0
        while index < len(command.args):
            arg = command.args[index]
            if arg == "--all":
                include_all = True
                index += 1
                continue
            if arg == "--page":
                if index + 1 >= len(command.args):
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N] [--all]",
                    )
                try:
                    page = int(command.args[index + 1])
                except ValueError:
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N] [--all]",
                    )
                index += 2
                continue
            if arg.startswith("--page="):
                try:
                    page = int(arg.partition("=")[2])
                except ValueError:
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N] [--all]",
                    )
                index += 1
                continue
            query_parts.append(arg)
            index += 1
        if page < 1:
            return CommandResponse(action="threads.invalid", text="Page number must be 1 or greater.")
        query = " ".join(part for part in query_parts if part).strip() or None
        return CommandResponse(
            action="threads.query",
            text="",
            include_all=include_all,
            payload={"page": page, "query": query},
        )

    def _handle_next(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del command
        context = self.store.get_thread_browser_context(channel_id, conversation_id)
        if context is None:
            return CommandResponse(action="threads.browser.missing", text="Use /threads first.")
        return CommandResponse(
            action="threads.query",
            text="",
            include_all=context.include_all,
            payload={"page": context.page + 1, "query": context.query},
        )

    def _handle_prev(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del command
        context = self.store.get_thread_browser_context(channel_id, conversation_id)
        if context is None:
            return CommandResponse(action="threads.browser.missing", text="Use /threads first.")
        return CommandResponse(
            action="threads.query",
            text="",
            include_all=context.include_all,
            payload={"page": max(1, context.page - 1), "query": context.query},
        )

    def _handle_pick(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        context = self.store.get_thread_browser_context(channel_id, conversation_id)
        if context is None:
            return CommandResponse(action="threads.browser.missing", text="Use /threads first.")
        if len(command.args) != 1:
            return CommandResponse(action="thread.pick.invalid", text="Usage: /pick <n>")
        try:
            index = int(command.args[0])
        except ValueError:
            return CommandResponse(action="thread.pick.invalid", text="Usage: /pick <n>")
        if index < 1 or index > len(context.thread_ids):
            return CommandResponse(action="thread.pick.invalid", text="Pick a number from the current page.")
        return CommandResponse(action="thread.pick", text="", payload={"index": index - 1})

    def _handle_exit(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del command
        context = self.store.get_thread_browser_context(channel_id, conversation_id)
        if context is None:
            return CommandResponse(action="threads.browser.missing", text="Use /threads first.")
        return CommandResponse(action="threads.exit", text="Closed thread list.")

    def _handle_thread(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        if len(command.args) == 1 and command.args[0] == "read":
            if binding.thread_id is None:
                return CommandResponse(action="thread.read.none", text="No active thread.")
            return CommandResponse(action="thread.read.query", text="", thread_id=binding.thread_id)
        if command.args and command.args[0] == "attach":
            selector = command.raw_args_text[len("attach") :].strip()
            if not selector:
                return CommandResponse(
                    action="thread.invalid",
                    text="Usage: /thread attach <thread-id-or-name> or /thread read",
                )
            return CommandResponse(
                action="thread.attach",
                text="",
                payload={"selector": selector},
            )
        return CommandResponse(
            action="thread.invalid",
            text="Usage: /thread attach <thread-id-or-name> or /thread read",
        )

    def _handle_new(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del command
        cwd = self.store.current_cwd(channel_id, conversation_id)
        if cwd is None:
            return CommandResponse(action="thread.new.missing_project", text="Choose a CWD first with /cwd <path>.")
        return CommandResponse(action="thread.new", text=f"Starting a thread in {cwd}.")

    def _handle_status(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id, command
        return CommandResponse(action="status.query", text="")

    def _handle_credits(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if command.args:
            return CommandResponse(action="credits.invalid", text="Usage: /credits")
        return CommandResponse(action="credits.read", text="")

    def _handle_stop(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del command
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.thread_id is None:
            return CommandResponse(action="turn.stop.none", text="No active turn to stop.")
        active = self.store.get_active_turn(binding.thread_id)
        if active is None:
            return CommandResponse(action="turn.stop.none", text="No active turn to stop.")
        return CommandResponse(action="turn.stop", text=f"Stopping turn {active[0]}.")

    def _handle_requests(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id, command
        return CommandResponse(action="unknown", text="Unknown command: /requests")

    def _handle_approve(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, command.args, "approval.accept", "accept")

    def _handle_deny(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, command.args, "approval.deny", "decline")

    def _handle_cancel(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        return self._handle_resolution(channel_id, conversation_id, command.args, "approval.cancel", "cancel")

    def _handle_answer(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if not command.args:
            return CommandResponse(action="request.answer.invalid", text="Usage: /answer <request-id> key=value ...")
        if "=" in command.args[0]:
            token = None
            answer_parts = command.args
        else:
            token = command.args[0]
            answer_parts = command.args[1:]
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

    def _handle_view(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if len(command.args) != 1 or command.args[0] not in {"minimal", "standard", "verbose"}:
            return CommandResponse(action="settings.view.invalid", text="Usage: /view minimal|standard|verbose")
        self.store.set_visibility_profile(channel_id, conversation_id, command.args[0])
        return CommandResponse(action="settings.view", text=f"Visibility profile set to {command.args[0]}.")

    def _handle_show(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, command.args, enabled=True)

    def _handle_hide(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        return self._handle_visibility_toggle(channel_id, conversation_id, command.args, enabled=False)

    def _handle_model(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="models.list", text="")
        if len(command.args) != 1:
            return CommandResponse(action="settings.model.invalid", text="Usage: /model [model-id]")
        if command.args[0] == "default":
            return CommandResponse(
                action="settings.model",
                text="Native default model cleared.",
                payload={"model": None},
            )
        return CommandResponse(
            action="settings.model",
            text=f"Native default model set to {command.args[0]}.",
            payload={"model": command.args[0]},
        )

    def _handle_models(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id, command
        return CommandResponse(action="models.list", text="")

    def _handle_think(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="settings.reasoning.read", text="")
        if len(command.args) != 1:
            return CommandResponse(
                action="settings.reasoning.invalid",
                text="Usage: /think [minimal|low|medium|high|xhigh|default]",
            )
        effort = command.args[0].lower()
        if effort == "default":
            return CommandResponse(
                action="settings.reasoning.write",
                text="Native reasoning effort cleared.",
                payload={"effort": None},
            )
        if effort not in _REASONING_EFFORTS:
            return CommandResponse(
                action="settings.reasoning.invalid",
                text="Usage: /think [minimal|low|medium|high|xhigh|default]",
            )
        return CommandResponse(
            action="settings.reasoning.write",
            text=f"Native reasoning effort set to {effort}.",
            payload={"effort": effort},
        )

    def _handle_fast(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args or command.args[0].lower() == "status":
            if len(command.args) > 1:
                return CommandResponse(action="settings.fast.invalid", text="Usage: /fast [on|off|status]")
            return CommandResponse(action="settings.fast.read", text="")
        if len(command.args) != 1:
            return CommandResponse(action="settings.fast.invalid", text="Usage: /fast [on|off|status]")
        mode = command.args[0].lower()
        if mode == "on":
            edits = [
                {"key_path": "service_tier", "value": "fast", "merge_strategy": "replace"},
                {"key_path": "features.fast_mode", "value": True, "merge_strategy": "replace"},
            ]
            return CommandResponse(
                action="settings.fast.write",
                text="Fast mode enabled.",
                payload={"mode": "on", "edits": edits},
            )
        if mode == "off":
            edits = [
                {"key_path": "service_tier", "value": "standard", "merge_strategy": "replace"},
                {"key_path": "features.fast_mode", "value": False, "merge_strategy": "replace"},
            ]
            return CommandResponse(
                action="settings.fast.write",
                text="Fast mode disabled.",
                payload={"mode": "off", "edits": edits},
            )
        return CommandResponse(action="settings.fast.invalid", text="Usage: /fast [on|off|status]")

    def _handle_permission(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="settings.permission.read", text="")
        if len(command.args) != 1:
            return CommandResponse(action="settings.permission.invalid", text="Usage: /permission [mode]")
        mode = command.args[0].lower()
        edits = _PERMISSION_PRESETS.get(mode)
        if edits is None:
            return CommandResponse(
                action="settings.permission.invalid",
                text="Usage: /permission [default|read-only|full-access]",
            )
        return CommandResponse(
            action="settings.permission.write",
            text=f"Permission mode set to {mode}.",
            payload={"mode": mode, "edits": edits},
        )

    def _handle_config(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(
                action="config.invalid",
                text="Usage: /config read [key] | /config write <key> <json-value> | /config batch <json>",
            )
        subcommand = command.args[0]
        if subcommand == "read":
            if len(command.args) > 2:
                return CommandResponse(action="config.read.invalid", text="Usage: /config read [key]")
            key_path = command.args[1] if len(command.args) == 2 else None
            return CommandResponse(action="config.read", text="", payload={"key_path": key_path})
        if subcommand == "write":
            raw = command.raw_args_text[len("write") :].strip()
            key_path, separator, value_text = raw.partition(" ")
            if not key_path or not separator:
                return CommandResponse(action="config.write.invalid", text="Usage: /config write <key> <json-value>")
            try:
                value = json.loads(value_text)
            except json.JSONDecodeError:
                return CommandResponse(action="config.write.invalid", text="Config value must be valid JSON.")
            return CommandResponse(
                action="config.write",
                text=f"Config key {key_path} updated.",
                payload={"key_path": key_path, "value": value},
            )
        if subcommand == "batch":
            raw = command.raw_args_text[len("batch") :].strip()
            if not raw:
                return CommandResponse(action="config.batch.invalid", text="Usage: /config batch <json>")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return CommandResponse(action="config.batch.invalid", text="Batch payload must be valid JSON.")
            if isinstance(parsed, dict):
                edits = parsed.get("edits")
                reload_user_config = bool(parsed.get("reloadUserConfig", False))
            else:
                edits = parsed
                reload_user_config = False
            if not isinstance(edits, list):
                return CommandResponse(action="config.batch.invalid", text="Batch payload must contain an edits array.")
            return CommandResponse(
                action="config.batch",
                text="Config batch update applied.",
                payload={"edits": edits, "reload_user_config": reload_user_config},
            )
        return CommandResponse(
            action="config.invalid",
            text="Usage: /config read [key] | /config write <key> <json-value> | /config batch <json>",
        )

    def _handle_native(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if not command.args or command.args[0] == "help":
            return CommandResponse(
                action="native.help",
                text="\n".join(
                    [
                        "Advanced native commands:",
                        "/native call <method> <json>",
                        "/native respond <request-id-or-prefix> <json>",
                        "/native error <request-id-or-prefix> <code> <message> [data-json]",
                        "/native events [filters...]",
                    ]
                ),
            )
        subcommand = command.args[0]
        if subcommand == "events":
            return CommandResponse(action="native.events", text="Native event journal is not available yet.")
        if subcommand == "call":
            raw = command.raw_args_text[len("call") :].strip()
            method, _, params_text = raw.partition(" ")
            if not method:
                return CommandResponse(action="native.call.invalid", text="Usage: /native call <method> <json>")
            if params_text.strip():
                try:
                    params = json.loads(params_text)
                except json.JSONDecodeError:
                    return CommandResponse(action="native.call.invalid", text="Native params must be valid JSON.")
                if not isinstance(params, dict):
                    return CommandResponse(action="native.call.invalid", text="Native params must be a JSON object.")
            else:
                params = {}
            return CommandResponse(action="native.call", text="", payload={"method": method, "params": params})
        if subcommand == "respond":
            raw = command.raw_args_text[len("respond") :].strip()
            token, _, payload_text = raw.partition(" ")
            if not token or not payload_text.strip():
                return CommandResponse(action="native.respond.invalid", text="Usage: /native respond <request-id-or-prefix> <json>")
            try:
                route = self.store.match_pending_request(channel_id, conversation_id, token)
            except ValueError as exc:
                return CommandResponse(action="native.respond.missing", text=str(exc))
            if route is None:
                return CommandResponse(action="native.respond.missing", text="Unknown native request.")
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                return CommandResponse(action="native.respond.invalid", text="Native response payload must be valid JSON.")
            if not isinstance(payload, dict):
                return CommandResponse(action="native.respond.invalid", text="Native response payload must be a JSON object.")
            return CommandResponse(
                action="native.respond",
                text=f"Responded to {route.request_id}.",
                request_id=route.request_id,
                payload=payload,
            )
        if subcommand == "error":
            if len(command.args) < 4:
                return CommandResponse(
                    action="native.error.invalid",
                    text="Usage: /native error <request-id-or-prefix> <code> <message> [data-json]",
                )
            token = command.args[1]
            try:
                route = self.store.match_pending_request(channel_id, conversation_id, token)
            except ValueError as exc:
                return CommandResponse(action="native.error.missing", text=str(exc))
            if route is None:
                return CommandResponse(action="native.error.missing", text="Unknown native request.")
            try:
                code = int(command.args[2])
            except ValueError:
                return CommandResponse(action="native.error.invalid", text="Native error code must be an integer.")
            message = command.args[3]
            data = None
            if len(command.args) > 4:
                try:
                    data = json.loads(" ".join(command.args[4:]))
                except json.JSONDecodeError:
                    return CommandResponse(action="native.error.invalid", text="Native error data must be valid JSON.")
            return CommandResponse(
                action="native.error",
                text=f"Returned error for {route.request_id}.",
                request_id=route.request_id,
                payload={"code": code, "message": message, "data": data},
            )
        return CommandResponse(action="native.invalid", text="Usage: /native help")

    def _handle_help(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id, command
        return CommandResponse(
            action="help",
            text="\n".join(
                [
                    "Core Commands",
                    "",
                    "/cwd <path>",
                    "Set the current working directory.",
                    "Use /cwd to view it, or /cwd playground to use a default folder.",
                    "",
                    "/threads",
                    "Browse and switch threads.",
                    "After opening the list, use /next, /prev, /pick <n>, or /exit.",
                    "",
                    "/new",
                    "Start a new thread in the current CWD.",
                    "",
                    "/status",
                    "Show the current CWD, thread, run state, model, permissions, and bridge visibility.",
                    "",
                    "/credits",
                    "Show current credits and rate-limit status.",
                    "",
                    "/stop",
                    "Stop the current running task.",
                    "",
                    "/model [model-id]",
                    "Leave it empty to browse models, or switch directly.",
                    "Examples: /model gpt-5.4, /model gpt-5.3-codex",
                    "",
                    "/think [effort]",
                    "Set reasoning effort.",
                    "Examples: /think low, /think xhigh, /think default",
                    "",
                    "/fast [on|off|status]",
                    "Toggle Fast mode.",
                    "",
                    "/permission [mode]",
                    "Leave it empty to browse permission modes, or switch directly.",
                    "Examples: /permission default, /permission full-access",
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
            routes = self.store.select_pending_requests(channel_id, conversation_id, token, kind="approval")
        except ValueError as exc:
            return CommandResponse(action=f"{action}.missing", text=str(exc))
        if not routes:
            return CommandResponse(action=f"{action}.missing", text="Unknown approval request.")
        request_ids = [route.request_id for route in routes]
        if len(request_ids) == 1:
            text = f"Recorded {decision} for {request_ids[0]}."
        else:
            text = f"Recorded {decision} for {len(request_ids)} requests."
        return CommandResponse(
            action=action,
            text=text,
            request_id=request_ids[0] if len(request_ids) == 1 else None,
            request_ids=request_ids,
        )

    def _handle_visibility_toggle(
        self,
        channel_id: str,
        conversation_id: str,
        args: list[str],
        *,
        enabled: bool,
    ) -> CommandResponse:
        if len(args) != 1 or args[0] not in {"commentary", "toolcalls", "system"}:
            return CommandResponse(action="settings.visibility.invalid", text="Usage: /show|/hide commentary|toolcalls|system")
        if args[0] == "commentary":
            self.store.set_commentary_visibility(channel_id, conversation_id, enabled=enabled)
            return CommandResponse(action="settings.visibility", text=f"Commentary messages {'shown' if enabled else 'hidden'}.")
        if args[0] == "toolcalls":
            self.store.set_toolcall_visibility(channel_id, conversation_id, enabled=enabled)
            return CommandResponse(action="settings.visibility", text=f"Tool-call messages {'shown' if enabled else 'hidden'}.")
        self.store.set_system_visibility(channel_id, conversation_id, enabled=enabled)
        return CommandResponse(action="settings.visibility", text=f"System messages {'shown' if enabled else 'hidden'}.")

    def _parse_answers(self, pairs: list[str]) -> dict[str, list[str]]:
        answers: dict[str, list[str]] = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            answers[key] = [part for part in value.split(",") if part]
        return answers

    def _default_playground_path(self) -> Path:
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            return desktop / "Codex Playground"
        if self.store.state_path is not None:
            return self.store.state_path.parent / "Codex Playground"
        return Path.cwd() / "Codex Playground"

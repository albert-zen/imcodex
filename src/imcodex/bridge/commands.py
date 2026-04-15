from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass

from ..store import ConversationStore


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
    def __init__(self, store: ConversationStore) -> None:
        self.store = store

    def handle(self, channel_id: str, conversation_id: str, text: str) -> CommandResponse:
        command = parse_command(text)
        handler = getattr(self, f"_handle_{command.name.replace('-', '_')}", None)
        if handler is None:
            return CommandResponse(action="unknown", text=f"Unknown command: /{command.name}")
        return handler(channel_id, conversation_id, command)

    def _handle_cwd(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if len(command.args) != 1:
            return CommandResponse(action="project.cwd.invalid", text="Usage: /cwd <path>")
        resolved = os.path.abspath(os.path.expanduser(command.args[0]))
        if not os.path.isdir(resolved):
            return CommandResponse(action="project.cwd.missing", text=f"Directory not found: {resolved}")
        self.store.set_bootstrap_cwd(channel_id, conversation_id, resolved)
        return CommandResponse(action="project.cwd", text=f"CWD set to {resolved}.")

    def _handle_threads(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        include_all = "--all" in command.args
        binding = self.store.get_binding(channel_id, conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None and not include_all:
            return CommandResponse(action="threads.missing_project", text="Choose a CWD first with /cwd <path>.")
        return CommandResponse(action="threads.query", text="", include_all=include_all)

    def _handle_thread(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        if len(command.args) == 1 and command.args[0] == "read":
            if binding.thread_id is None:
                return CommandResponse(action="thread.read.none", text="No active thread.")
            return CommandResponse(action="thread.read.query", text="", thread_id=binding.thread_id)
        if len(command.args) == 2 and command.args[0] == "attach":
            return CommandResponse(action="thread.attach", text=f"Attaching thread {command.args[1]}.", thread_id=command.args[1])
        return CommandResponse(
            action="thread.invalid",
            text="Usage: /thread attach <thread-id> or /thread read",
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
        del command
        requests = self.store.list_pending_requests(channel_id, conversation_id)
        if not requests:
            return CommandResponse(action="requests.list", text="No pending requests.")
        lines = ["Pending requests:"]
        for route in requests:
            handle = route.request_handle or route.request_id[:8]
            lines.append(f"- [{handle}] {route.kind}: {route.request_id}")
        return CommandResponse(action="requests.list", text="\n".join(lines))

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
        if len(command.args) != 1:
            return CommandResponse(action="settings.model.invalid", text="Usage: /model <name|default>")
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
                        "/native requests",
                        "/native events [filters...]",
                    ]
                ),
            )
        subcommand = command.args[0]
        if subcommand == "requests":
            return self._handle_requests(channel_id, conversation_id, command)
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
                    "IMCodex Commands",
                    "",
                    "Thread",
                    "/cwd <path>",
                    "/status",
                    "/new",
                    "/stop",
                    "/threads",
                    "/thread attach <thread-id>",
                    "/thread read",
                    "",
                    "Model & Config",
                    "/model <name|default>",
                    "/models",
                    "/config read [key]",
                    "/config write <key> <json-value>",
                    "/config batch <json>",
                    "",
                    "Requests",
                    "/requests",
                    "/approve [request-id-or-prefix]",
                    "/deny [request-id-or-prefix]",
                    "/cancel [request-id-or-prefix]",
                    "/answer [request-id-or-prefix] key=value ...",
                    "",
                    "View",
                    "/view minimal|standard|verbose",
                    "/show commentary|toolcalls|system",
                    "/hide commentary|toolcalls|system",
                    "",
                    "Advanced",
                    "/native help",
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

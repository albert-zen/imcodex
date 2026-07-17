from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..store import ConversationStore


_PERMISSION_MODES = {"default", "read-only", "full-access"}
_PERSONALITIES = {"none", "friendly", "pragmatic"}
_MAX_GOAL_OBJECTIVE_CHARS = 4000
_DEFAULT_HISTORY_TURNS = 1
_MAX_HISTORY_TURNS = 5


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
        page = 1
        query_parts: list[str] = []
        index = 0
        while index < len(command.args):
            arg = command.args[index]
            if arg == "--page":
                if index + 1 >= len(command.args):
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N]",
                    )
                try:
                    page = int(command.args[index + 1])
                except ValueError:
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N]",
                    )
                index += 2
                continue
            if arg.startswith("--page="):
                try:
                    page = int(arg.partition("=")[2])
                except ValueError:
                    return CommandResponse(
                        action="threads.invalid",
                        text="Usage: /threads [query] [--page N]",
                    )
                index += 1
                continue
            if arg.startswith("--"):
                return CommandResponse(
                    action="threads.invalid",
                    text="Usage: /threads [query] [--page N]",
                )
            query_parts.append(arg)
            index += 1
        if page < 1:
            return CommandResponse(action="threads.invalid", text="Page number must be 1 or greater.")
        query = " ".join(part for part in query_parts if part).strip() or None
        return CommandResponse(
            action="threads.query",
            text="",
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
            payload={"page": max(1, context.page - 1), "query": context.query},
        )

    def _handle_pick(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        context = self.store.get_thread_browser_context(channel_id, conversation_id)
        if context is None:
            return CommandResponse(action="threads.browser.missing", text="Use /threads first.")
        if not command.args:
            return CommandResponse(
                action="thread.pick.invalid",
                text="Usage: /pick <n> [--history [N]]",
            )
        try:
            index = int(command.args[0])
        except ValueError:
            return CommandResponse(
                action="thread.pick.invalid",
                text="Usage: /pick <n> [--history [N]]",
            )
        if index < 1 or index > len(context.thread_ids):
            return CommandResponse(action="thread.pick.invalid", text="Pick a number from the current page.")
        history_limit = self._parse_pick_history(command.args[1:])
        if isinstance(history_limit, CommandResponse):
            return history_limit
        payload: dict[str, object] = {"index": index - 1}
        if history_limit is not None:
            payload["history_limit"] = history_limit
        return CommandResponse(action="thread.pick", text="", payload=payload)

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
        if command.args and command.args[0] == "history":
            return self._history_response(
                binding.thread_id,
                command.args[1:],
                usage="Usage: /thread history [N]",
            )
        if command.args and command.args[0] == "attach":
            selector = command.raw_args_text[len("attach") :].strip()
            if not selector:
                return CommandResponse(
                    action="thread.invalid",
                    text="Usage: /thread attach <thread-id-or-name> | /thread read | /thread history [N]",
                )
            return CommandResponse(
                action="thread.attach",
                text="",
                payload={"selector": selector},
            )
        return CommandResponse(
            action="thread.invalid",
            text="Usage: /thread attach <thread-id-or-name> | /thread read | /thread history [N]",
        )

    def _handle_history(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        binding = self.store.get_binding(channel_id, conversation_id)
        return self._history_response(
            binding.thread_id,
            command.args,
            usage="Usage: /history [N]",
        )

    def _history_response(
        self,
        thread_id: str | None,
        args: list[str],
        *,
        usage: str,
    ) -> CommandResponse:
        if thread_id is None:
            return CommandResponse(action="thread.history.missing", text="No active thread.")
        limit = self._parse_history_limit(args, usage=usage)
        if isinstance(limit, CommandResponse):
            return limit
        return CommandResponse(
            action="thread.history.query",
            text="",
            thread_id=thread_id,
            payload={"limit": limit},
        )

    def _parse_pick_history(self, args: list[str]) -> int | None | CommandResponse:
        if not args:
            return None
        if len(args) == 1 and args[0] == "--history":
            return _DEFAULT_HISTORY_TURNS
        if len(args) == 1 and args[0].startswith("--history="):
            value = args[0].partition("=")[2]
            return self._parse_history_limit(
                [value],
                usage="Usage: /pick <n> [--history [N]]",
                action="thread.pick.invalid",
            )
        if len(args) == 2 and args[0] == "--history":
            return self._parse_history_limit(
                [args[1]],
                usage="Usage: /pick <n> [--history [N]]",
                action="thread.pick.invalid",
            )
        return CommandResponse(
            action="thread.pick.invalid",
            text="Usage: /pick <n> [--history [N]]",
        )

    def _parse_history_limit(
        self,
        args: list[str],
        *,
        usage: str,
        action: str = "thread.history.invalid",
    ) -> int | CommandResponse:
        if not args:
            return _DEFAULT_HISTORY_TURNS
        if len(args) != 1:
            return CommandResponse(action=action, text=usage)
        try:
            limit = int(args[0])
        except ValueError:
            return CommandResponse(action=action, text=usage)
        if limit < 1 or limit > _MAX_HISTORY_TURNS:
            return CommandResponse(
                action=action,
                text=f"History turns must be between 1 and {_MAX_HISTORY_TURNS}.",
            )
        return limit

    def _handle_fork(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if command.args:
            return CommandResponse(action="thread.fork.invalid", text="Usage: /fork")
        if self.store.get_binding(channel_id, conversation_id).thread_id is None:
            return CommandResponse(action="thread.fork.missing", text="No active thread.")
        return CommandResponse(action="thread.fork", text="Forking thread.")

    def _handle_rename(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if self.store.get_binding(channel_id, conversation_id).thread_id is None:
            return CommandResponse(action="thread.rename.missing", text="No active thread.")
        name = self._strip_matching_quotes(command.raw_args_text).strip()
        if not name:
            return CommandResponse(action="thread.rename.invalid", text="Usage: /rename <name>")
        return CommandResponse(action="thread.rename", text="", payload={"name": name})

    def _handle_compact(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        if command.args:
            return CommandResponse(action="thread.compact.invalid", text="Usage: /compact")
        if self.store.get_binding(channel_id, conversation_id).thread_id is None:
            return CommandResponse(action="thread.compact.missing", text="No active thread.")
        return CommandResponse(action="thread.compact", text="Starting compaction.")

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

    def _handle_goal(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="goal.read", text="")
        if len(command.args) == 1:
            subcommand = command.args[0].lower()
            if subcommand == "clear":
                return CommandResponse(action="goal.clear", text="Clearing goal.")
            if subcommand == "pause":
                return CommandResponse(action="goal.status", text="Pausing goal.", payload={"status": "paused"})
            if subcommand == "resume":
                return CommandResponse(action="goal.status", text="Resuming goal.", payload={"status": "active"})
        objective = self._strip_matching_quotes(command.raw_args_text).strip()
        if not objective:
            return CommandResponse(action="goal.invalid", text="Usage: /goal [pause|resume|clear|<objective>]")
        if len(objective) > _MAX_GOAL_OBJECTIVE_CHARS:
            return CommandResponse(
                action="goal.invalid",
                text="Goal objective must be at most 4000 characters.",
            )
        return CommandResponse(
            action="goal.set",
            text="Setting goal.",
            payload={"objective": objective},
        )

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
                text="Usage: /think [effort|default]",
            )
        effort = command.args[0].lower()
        if effort == "default":
            return CommandResponse(
                action="settings.reasoning.write",
                text="Native reasoning effort preference cleared. It applies to new threads; resumed threads retain their native setting.",
                payload={"effort": None},
            )
        return CommandResponse(
            action="settings.reasoning.write",
            text=f"Native reasoning effort preference set to {effort}. It applies to new threads; resumed threads retain their native setting.",
            payload={"effort": effort},
        )

    def _handle_personality(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="settings.personality.read", text="")
        if len(command.args) != 1:
            return CommandResponse(
                action="settings.personality.invalid",
                text="Usage: /personality [default|none|friendly|pragmatic]",
            )
        personality = command.args[0].lower()
        if personality == "default":
            return CommandResponse(
                action="settings.personality.write",
                text="Native personality preference reset to default. It applies to new threads; resumed threads retain their native setting.",
                payload={"personality": None},
            )
        if personality not in _PERSONALITIES:
            return CommandResponse(
                action="settings.personality.invalid",
                text="Usage: /personality [default|none|friendly|pragmatic]",
            )
        return CommandResponse(
            action="settings.personality.write",
            text=f"Native personality preference set to {personality}. It applies to new threads; resumed threads retain their native setting.",
            payload={"personality": personality},
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
            return CommandResponse(
                action="settings.fast.write",
                text="Fast mode enabled.",
                payload={"enabled": True},
            )
        if mode == "off":
            return CommandResponse(
                action="settings.fast.write",
                text="Fast mode disabled.",
                payload={"enabled": False},
            )
        return CommandResponse(action="settings.fast.invalid", text="Usage: /fast [on|off|status]")

    def _handle_permission(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id
        if not command.args:
            return CommandResponse(action="settings.permission.read", text="")
        if len(command.args) != 1:
            return CommandResponse(action="settings.permission.invalid", text="Usage: /permission [mode]")
        mode = command.args[0].lower()
        if mode not in _PERMISSION_MODES:
            return CommandResponse(
                action="settings.permission.invalid",
                text="Usage: /permission [default|read-only|full-access]",
            )
        return CommandResponse(
            action="settings.permission.write",
            text=f"Permission mode set to {mode}.",
            payload={"mode": mode},
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
            return self._handle_native_events(command.args[1:])
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

    def _handle_native_events(self, args: list[str]) -> CommandResponse:
        filters: list[str] = []
        limit = 12
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"--limit", "-n"}:
                if index + 1 >= len(args):
                    return CommandResponse(action="native.events.invalid", text="Usage: /native events [filters...] [--limit N]")
                try:
                    limit = int(args[index + 1])
                except ValueError:
                    return CommandResponse(action="native.events.invalid", text="Native event limit must be an integer.")
                index += 2
                continue
            if arg.startswith("--limit="):
                try:
                    limit = int(arg.partition("=")[2])
                except ValueError:
                    return CommandResponse(action="native.events.invalid", text="Native event limit must be an integer.")
                index += 1
                continue
            if arg.startswith("--"):
                return CommandResponse(action="native.events.invalid", text="Usage: /native events [filters...] [--limit N]")
            filters.append(arg)
            index += 1
        if limit < 1:
            return CommandResponse(action="native.events.invalid", text="Native event limit must be at least 1.")
        return CommandResponse(
            action="native.events",
            text="",
            payload={"filters": filters, "limit": limit},
        )

    def _handle_help(self, channel_id: str, conversation_id: str, command: ParsedCommand) -> CommandResponse:
        del channel_id, conversation_id, command
        return CommandResponse(
            action="help",
            text="\n".join(
                [
                    "Help",
                    "",
                    "Start",
                    "/cwd <path>",
                    "Choose a workspace.",
                    "/new",
                    "Start a fresh thread.",
                    "",
                    "Threads",
                    "/threads [query]",
                    "Browse and switch threads.",
                    "/history [turns]",
                    "Show recent turns.",
                    "/fork",
                    "Continue from a copy.",
                    "/rename <name>",
                    "Name the current thread.",
                    "/compact",
                    "Compact current thread context.",
                    "",
                    "Run",
                    "/status",
                    "Show current thread, model, permissions, and state.",
                    "/stop",
                    "Stop the running task.",
                    "/goal [objective|pause|resume|clear]",
                    "View or set the thread goal.",
                    "",
                    "Settings",
                    "/model [model-id]",
                    "Browse or switch model.",
                    "/think [effort]",
                    "Set reasoning effort.",
                    "/personality [style]",
                    "Browse or switch personality.",
                    "/fast [on|off|status]",
                    "Toggle fast mode.",
                    "/permission [mode]",
                    "Browse or switch permission mode.",
                    "",
                    "Account",
                    "/credits",
                    "Show usage, credits, and rate limits.",
                    "",
                    "Advanced",
                    "/native help",
                    "Diagnostics and native escape hatches.",
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

    def _strip_matching_quotes(self, value: str) -> str:
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        return text

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

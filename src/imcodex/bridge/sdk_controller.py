from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imagent import CommandRegistry, CommandResult
from imagent.applications import ApplicationRef, ProjectRef, ThreadRef
from imagent.gateway.outcomes import Failed, OutcomeUnknown, Partial, Succeeded
from imagent.applications.requests import ApprovalRequest, UserInputRequest
from imagent.interaction.controllers import (
    CommandArgumentContract,
    CommandDefinition,
    CommandExecutionSafety,
    CommandInvocation,
    InboundController,
    include_common_commands,
)
from imagent.interaction.messages import (
    InboundMessage,
    OutboundMessage,
    TextContent,
    TextFormat,
)


class ImcodexController:
    """IMCodex command and onboarding policy over public SDK actions.

    The controller owns only product commands and presentation preferences.
    Binding, thread, turn, request, transcript, and projection mutations go
    through the scoped SDK actions supplied by Gateway.
    """

    _PROJECT_COMMANDS = frozenset(
        {
            "new",
            "use",
            "project",
            "threads",
            "pick",
            "thread",
            "delete",
            "archive",
            "status",
            "catchup",
            "history",
        }
    )
    _THREAD_COMMANDS = frozenset(
        {
            "stop",
            "rename",
            "compact",
            "fork",
            "goal",
        }
    )

    def __init__(
        self,
        *,
        client,
        product_state,
        request_presenter=None,
        application_instance_id: str = "codex-main",
        workspace_id: str = "imcodex-workspace",
    ) -> None:
        self.client = client
        self.product_state = product_state
        self.request_presenter = request_presenter
        self.application_ref = ApplicationRef(application_instance_id)
        self.workspace_project_ref = ProjectRef(application_instance_id, workspace_id)
        self.registry = self._build_registry()

    def _build_registry(self) -> CommandRegistry:
        registry = CommandRegistry()
        registry.register(
            CommandDefinition(
                name="help",
                aliases=("start",),
                handler=self._help,
                arguments=CommandArgumentContract(0, 0),
                summary="Show IMCodex commands.",
                usage="/help",
            )
        )
        include_common_commands(
            registry,
            names=(
                "apps",
                "app",
                "projects",
                "use",
                "threads",
                "pick",
                "new",
                "delete",
                "status",
                "catchup",
                "history",
                "respond",
            ),
        )
        self._register_product_commands(registry)
        registry.freeze()
        registry.validate_startup()
        return registry

    def _register_product_commands(self, registry: CommandRegistry) -> None:
        read_only = CommandExecutionSafety.READ_ONLY
        effectful = CommandExecutionSafety.EFFECTFUL

        def add(
            name: str,
            handler,
            *,
            aliases: tuple[str, ...] = (),
            minimum: int = 0,
            maximum: int = 8,
            safety: CommandExecutionSafety = read_only,
            summary: str = "",
            usage: str = "",
        ) -> None:
            registry.register(
                CommandDefinition(
                    name=name,
                    aliases=aliases,
                    handler=handler,
                    arguments=CommandArgumentContract(minimum, maximum),
                    safety=safety,
                    summary=summary,
                    usage=usage,
                )
            )

        add("cwd", self._cwd, maximum=1, safety=effectful, usage="/cwd [path]")
        add("view", self._view, minimum=1, maximum=1, safety=effectful, usage="/view minimal|standard|verbose")
        add("show", self._show_hide, minimum=1, maximum=1, safety=effectful, usage="/show commentary|toolcalls|system")
        add("hide", self._show_hide, minimum=1, maximum=1, safety=effectful, usage="/hide commentary|toolcalls|system")
        add("stop", self._stop, safety=effectful, usage="/stop")
        add("rename", self._rename, minimum=1, maximum=8, safety=effectful, usage="/rename <name>")
        add("compact", self._compact, safety=effectful, usage="/compact")
        add("fork", self._fork, safety=effectful, usage="/fork")
        add("goal", self._goal, maximum=32, safety=effectful, usage="/goal [pause|resume|clear|<objective>]")
        add("model", self._model, aliases=("models",), maximum=1, safety=effectful, usage="/model [model-id]")
        add("think", self._think, maximum=1, safety=effectful, usage="/think [effort|default]")
        add("personality", self._personality, maximum=1, safety=effectful, usage="/personality [name|default]")
        add("fast", self._fast, maximum=1, safety=effectful, usage="/fast [on|off]")
        add("permission", self._permission, maximum=1, safety=effectful, usage="/permission [default|read-only|full-access]")
        add("credits", self._credits, maximum=2, safety=effectful, usage="/credits [reset [credit-id]]")
        add("config", self._config, maximum=2, safety=effectful, usage="/config [key=value]")
        add("native", self._native, maximum=1, safety=read_only, usage="/native")
        add("approve", self._approve, maximum=1, safety=effectful, usage="/approve [request-id]")
        add("deny", self._deny, maximum=1, safety=effectful, usage="/deny [request-id]")
        add("cancel", self._cancel, maximum=1, safety=effectful, usage="/cancel [request-id]")
        add("answer", self._answer, minimum=2, maximum=32, safety=effectful, usage="/answer [request-id] key=value ...")

    async def handle(
        self,
        message: InboundMessage,
        actions,
    ) -> tuple[OutboundMessage, ...] | None:
        text = self._text(message)
        command_name = self._command_name(text)
        try:
            if command_name is None:
                await self._cancel_pending_requests(message, actions)
                await self._ensure_thread(actions, message.message_id)
                return None
            if command_name in self._PROJECT_COMMANDS:
                await self._ensure_project(actions, message.message_id)
            elif command_name in self._THREAD_COMMANDS:
                await self._ensure_thread(actions, message.message_id)
            return await self.registry.handle(message, actions)
        except Exception as exc:
            return (self._reply(message, self._safe_error(exc)),)

    def validate_startup(self) -> None:
        self.registry.validate_startup()

    async def close(self) -> None:
        await self.registry.close()

    async def _help(self, invocation: CommandInvocation, _actions) -> CommandResult:
        del invocation
        return CommandResult.text(
            "\n".join(
                (
                    "**IMCodex commands**",
                    "`/help` `/cwd` `/new` `/threads` `/pick` `/history` `/status`",
                    "`/stop` `/fork` `/rename` `/compact` `/goal`",
                    "`/model` `/think` `/personality` `/fast` `/permission`",
                    "`/view` `/show` `/hide` `/credits` `/config` `/native`",
                    "SDK navigation: `/apps` `/app` `/projects` `/use` `/respond` `/answer`.",
                )
            )
        )

    async def _cwd(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await actions.get_binding()
        current = await self._project_root(binding, actions)
        if not invocation.arguments:
            return CommandResult.text(f"Current CWD: `{current or '(none)'}`.")
        requested = str(Path(invocation.arguments[0]).expanduser().absolute())
        if current and Path(requested) == Path(current).absolute():
            return CommandResult.text(f"Current CWD: `{current}`.")
        return CommandResult.failure(
            "This SDK Codex Application is configured with one workspace CWD. "
            "Changing CWD per conversation is unsupported; restart IMCodex with the desired workspace."
        )

    async def _view(self, invocation: CommandInvocation, _actions) -> CommandResult:
        value = invocation.arguments[0].casefold()
        profiles = {
            "minimal": {
                "show_commentary": False,
                "show_toolcalls": False,
                "show_system": False,
            },
            "standard": {
                "show_commentary": True,
                "show_toolcalls": False,
                "show_system": False,
            },
            "verbose": {
                "show_commentary": True,
                "show_toolcalls": True,
                "show_system": True,
            },
        }
        if value not in profiles:
            return CommandResult.failure("Usage: /view minimal|standard|verbose")
        self.product_state.update(
            invocation.conversation_ref.channel_instance_id,
            invocation.conversation_ref.native_conversation_id,
            visibility=value,
            **profiles[value],
        )
        return CommandResult.text(f"Visibility profile set to `{value}`.")

    async def _show_hide(self, invocation: CommandInvocation, _actions) -> CommandResult:
        field = invocation.arguments[0].casefold()
        names = {"commentary": "show_commentary", "toolcalls": "show_toolcalls", "system": "show_system"}
        if field not in names:
            return CommandResult.failure("Usage: /show|hide commentary|toolcalls|system")
        self.product_state.update(
            invocation.conversation_ref.channel_instance_id,
            invocation.conversation_ref.native_conversation_id,
            **{names[field]: invocation.command_name == "show"},
        )
        state = "shown" if invocation.command_name == "show" else "hidden"
        return CommandResult.text(f"{field} output is now {state}.")

    async def _stop(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await self._require_thread_binding(actions)
        result = await actions.interrupt_turn(
            binding.thread_ref,
            action_id=self._action_id(invocation, "turn.interrupt"),
        )
        self._require_success(result)
        return CommandResult.text("Active turn interrupted.")

    async def _rename(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await self._require_thread_binding(actions)
        name = " ".join(invocation.arguments).strip()
        await self.client.set_thread_name(binding.thread_ref.thread_id, name)
        return CommandResult.text(f"Renamed thread to `{name}`.")

    async def _compact(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await self._require_thread_binding(actions)
        await self.client.compact_thread(binding.thread_ref.thread_id)
        return CommandResult.text("Compaction started.")

    async def _fork(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await self._require_thread_binding(actions)
        result = await self.client.fork_thread(binding.thread_ref.thread_id)
        thread_id = self._thread_id_from_payload(result)
        if not thread_id:
            return CommandResult.failure("Codex did not return the forked thread identity.")
        thread_ref = ThreadRef(binding.project_ref, thread_id)
        self._require_success(
            await actions.bind_thread(
                thread_ref,
                action_id=self._action_id(invocation, "thread.bind"),
            )
        )
        self._require_success(
            await actions.observe_thread(
                thread_ref,
                action_id=self._action_id(invocation, "thread.observe"),
                reply_to_message_id=invocation.message_id,
            )
        )
        return CommandResult.text(f"Forked and selected thread `{thread_id}`.")

    async def _goal(self, invocation: CommandInvocation, actions) -> CommandResult:
        binding = await self._require_thread_binding(actions)
        thread_id = binding.thread_ref.thread_id
        if not invocation.arguments:
            return CommandResult.text(self._render_json(await self.client.get_thread_goal(thread_id)))
        if len(invocation.arguments) == 1 and invocation.arguments[0].casefold() == "clear":
            result = await self.client.clear_thread_goal(thread_id)
            return CommandResult.text(self._render_json(result))
        if len(invocation.arguments) == 1 and invocation.arguments[0].casefold() in {"pause", "resume"}:
            status = "paused" if invocation.arguments[0].casefold() == "pause" else "active"
            result = await self.client.set_thread_goal(thread_id, status=status)
            return CommandResult.text(self._render_json(result))
        objective = " ".join(invocation.arguments).strip()
        if len(objective) > 4000:
            return CommandResult.failure("Goal objective must be at most 4000 characters.")
        result = await self.client.set_thread_goal(thread_id, objective=objective, status="active")
        return CommandResult.text(self._render_json(result))

    async def _model(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            return CommandResult.text(self._render_models(await self.client.list_models()))
        model = invocation.arguments[0]
        value = None if model.casefold() == "default" else model
        await self.client.write_config_value(key_path="model", value=value)
        return CommandResult.text("Native default model cleared." if value is None else f"Native default model set to `{value}`.")

    async def _think(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            config = await self.client.read_config()
            return CommandResult.text(self._render_config_value(config, "model_reasoning_effort", "reasoningEffort"))
        value = None if invocation.arguments[0].casefold() == "default" else invocation.arguments[0]
        await self.client.write_config_value(key_path="model_reasoning_effort", value=value)
        return CommandResult.text("Reasoning effort preference cleared." if value is None else f"Reasoning effort set to `{value}`.")

    async def _personality(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            config = await self.client.read_config()
            return CommandResult.text(self._render_config_value(config, "personality"))
        value = None if invocation.arguments[0].casefold() == "default" else invocation.arguments[0].casefold()
        if value not in {None, "none", "friendly", "pragmatic"}:
            return CommandResult.failure("Personality must be default, none, friendly, or pragmatic.")
        await self.client.write_config_value(key_path="personality", value=value)
        return CommandResult.text("Personality preference cleared." if value is None else f"Personality set to `{value}`.")

    async def _fast(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            config = await self.client.read_config()
            return CommandResult.text(self._render_config_value(config, "service_tier", "serviceTier"))
        value = invocation.arguments[0].casefold()
        if value not in {"on", "off", "true", "false", "default"}:
            return CommandResult.failure("Usage: /fast [on|off]")
        enabled = value in {"on", "true"}
        tier = "priority" if enabled else "default"
        await self.client.write_config_value(key_path="service_tier", value=tier)
        return CommandResult.text(f"Fast mode {'enabled' if enabled else 'disabled'}.")

    async def _permission(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            config = await self.client.read_config()
            approval = self._config_value(config, "approval_policy", "approvalPolicy")
            sandbox = self._config_value(config, "sandbox_mode", "sandboxMode")
            mode = {
                ("on-request", "workspace-write"): "default",
                ("on-request", "read-only"): "read-only",
                ("never", "danger-full-access"): "full-access",
            }.get((approval, sandbox), "custom")
            return CommandResult.text(
                f"Permission mode: `{mode}` "
                f"(approval: `{approval or '(unset)'}`, sandbox: `{sandbox or '(unset)'}`)."
            )
        mode = invocation.arguments[0].casefold()
        values = {
            "default": ("on-request", "workspace-write"),
            "read-only": ("on-request", "read-only"),
            "full-access": ("never", "danger-full-access"),
        }
        if mode not in values:
            return CommandResult.failure("Permission must be default, read-only, or full-access.")
        approval, sandbox = values[mode]
        await self.client.batch_write_config(
            edits=[
                {
                    "keyPath": "approval_policy",
                    "value": approval,
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "sandbox_mode",
                    "value": sandbox,
                    "mergeStrategy": "replace",
                },
            ],
            reload_user_config=True,
        )
        return CommandResult.text(f"Permission mode set to `{mode}`.")

    async def _credits(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if invocation.arguments and invocation.arguments[0].casefold() == "reset":
            credit_id = invocation.arguments[1] if len(invocation.arguments) > 1 else None
            result = await self.client.consume_account_rate_limit_reset_credit(
                idempotency_key=self._action_id(invocation, "credits.reset"),
                credit_id=credit_id,
            )
            return CommandResult.text(self._render_json(result))
        return CommandResult.text(self._render_json(await self.client.read_account_rate_limits()))

    async def _config(self, invocation: CommandInvocation, _actions) -> CommandResult:
        if not invocation.arguments:
            return CommandResult.text(self._render_json(await self.client.read_config()))
        if len(invocation.arguments) != 1 or "=" not in invocation.arguments[0]:
            return CommandResult.failure("Usage: /config [key=value]")
        key, _, value = invocation.arguments[0].partition("=")
        if not key:
            return CommandResult.failure("Configuration key must not be empty.")
        await self.client.write_config_value(key_path=key, value=value)
        return CommandResult.text(f"Native configuration `{key}` updated.")

    async def _native(self, invocation: CommandInvocation, _actions) -> CommandResult:
        del invocation
        facts = dict(self.client.connection_facts())
        if "endpoint" in facts:
            facts["endpoint"] = "(redacted by IMCodex)"
        return CommandResult.text(self._render_json(facts))

    async def _approve(self, invocation: CommandInvocation, actions) -> CommandResult:
        return await self._respond_approval(invocation, actions, "approve")

    async def _deny(self, invocation: CommandInvocation, actions) -> CommandResult:
        return await self._respond_approval(invocation, actions, "deny")

    async def _cancel(self, invocation: CommandInvocation, actions) -> CommandResult:
        return await self._respond_approval(invocation, actions, "cancel")

    async def _respond_approval(
        self,
        invocation: CommandInvocation,
        actions,
        operation: str,
    ) -> CommandResult:
        presenter = self._require_request_presenter()
        token = invocation.arguments[0] if invocation.arguments else None
        request = presenter.match(invocation.conversation_ref, token, kind="approval")
        if not isinstance(request, ApprovalRequest):
            raise RuntimeError("SDK request type changed while responding")
        result = await actions.respond_request(
            request.request_ref,
            presenter.approval_response(request, operation),
            action_id=self._action_id(invocation, f"request.{operation}"),
        )
        self._require_success(result)
        presenter.resolved(invocation.conversation_ref, request)
        return CommandResult.text(
            f"Recorded {operation} for `{request.request_ref.native_request_id}`."
        )

    async def _answer(self, invocation: CommandInvocation, actions) -> CommandResult:
        presenter = self._require_request_presenter()
        arguments = list(invocation.arguments)
        token = None if "=" in arguments[0] else arguments.pop(0)
        if not arguments:
            return CommandResult.failure("Usage: /answer [request-id] key=value ...")
        answers: dict[str, list[str]] = {}
        for item in arguments:
            key, separator, value = item.partition("=")
            if not separator or not key or not value:
                return CommandResult.failure("Usage: /answer [request-id] key=value ...")
            answers.setdefault(key, []).append(value)
        request = presenter.match(invocation.conversation_ref, token, kind="user_input")
        if not isinstance(request, UserInputRequest):
            raise RuntimeError("SDK request type changed while responding")
        result = await actions.respond_request(
            request.request_ref,
            presenter.user_input_response(
                request,
                {key: tuple(values) for key, values in answers.items()},
            ),
            action_id=self._action_id(invocation, "request.answer"),
        )
        self._require_success(result)
        presenter.resolved(invocation.conversation_ref, request)
        return CommandResult.text(
            f"Recorded answer for `{request.request_ref.native_request_id}`."
        )

    async def _cancel_pending_requests(self, message: InboundMessage, actions) -> None:
        presenter = self.request_presenter
        if presenter is None:
            return
        for request in presenter.approvals(message.conversation_ref):
            result = await actions.respond_request(
                request.request_ref,
                presenter.approval_response(request, "cancel"),
                action_id=f"imcodex:{message.message_id}:request.cancel:{request.request_ref.native_request_id}",
            )
            self._require_success(result)
            presenter.resolved(message.conversation_ref, request)

    def _require_request_presenter(self):
        if self.request_presenter is None:
            raise RuntimeError("Interactive request presentation is unavailable")
        return self.request_presenter

    async def _ensure_project(self, actions, message_id: str) -> None:
        binding = await actions.get_binding()
        if binding is None or binding.application_ref is None:
            self._require_success(
                await actions.select_application(
                    self.application_ref,
                    action_id=f"imcodex:{message_id}:application.select",
                )
            )
            binding = await actions.get_binding()
        if binding is None or binding.project_ref is None:
            self._require_success(
                await actions.select_project(
                    self.workspace_project_ref,
                    action_id=f"imcodex:{message_id}:project.select",
                )
            )

    async def _ensure_thread(self, actions, message_id: str) -> None:
        await self._ensure_project(actions, message_id)
        binding = await actions.get_binding()
        if binding is not None and binding.thread_ref is not None:
            return
        if binding is None or binding.project_ref is None:
            raise RuntimeError("SDK did not expose a selected project")
        result = await actions.create_and_bind_thread(
            binding.project_ref,
            action_id=f"imcodex:{message_id}:thread.create",
            title="IM task",
        )
        value = self._require_success(result)
        thread_ref = getattr(value, "ref", None)
        if not isinstance(thread_ref, ThreadRef):
            raise RuntimeError("SDK did not return the created thread identity")
        self._require_success(
            await actions.observe_thread(
                thread_ref,
                action_id=f"imcodex:{message_id}:thread.observe",
                reply_to_message_id=message_id,
            )
        )

    async def _require_thread_binding(self, actions):
        binding = await actions.get_binding()
        if binding is None or binding.thread_ref is None or binding.project_ref is None:
            raise RuntimeError("No active thread. Use /new first.")
        return binding

    async def _project_root(self, binding, actions) -> str | None:
        if binding is None or binding.project_ref is None:
            return None
        result = await actions.get_project(binding.project_ref)
        value = self._require_success(result)
        return str(getattr(value, "root_path", None) or "") or None

    @staticmethod
    def _require_success(result):
        if isinstance(result, Succeeded):
            return result.value
        if isinstance(result, Partial):
            raise RuntimeError(f"SDK action completed partially: {result.error}")
        if isinstance(result, OutcomeUnknown):
            raise RuntimeError(f"SDK action outcome is unknown: {result.error}")
        if isinstance(result, Failed):
            raise RuntimeError(str(result.error))
        raise RuntimeError("SDK action returned an incompatible result")

    @staticmethod
    def _thread_id_from_payload(payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("threadId", "thread_id", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                nested = ImcodexController._thread_id_from_payload(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for value in payload:
                nested = ImcodexController._thread_id_from_payload(value)
                if nested:
                    return nested
        return None

    @staticmethod
    def _text(message: InboundMessage) -> str:
        return "\n".join(item.text for item in message.content if isinstance(item, TextContent)).strip()

    @staticmethod
    def _command_name(text: str) -> str | None:
        if not text.startswith("/"):
            return None
        try:
            parts = shlex.split(text.splitlines()[0])
        except ValueError:
            return ""
        return parts[0][1:].casefold() if parts and parts[0].startswith("/") else ""

    @staticmethod
    def _action_id(invocation: CommandInvocation, suffix: str) -> str:
        return f"imcodex:{invocation.invocation_id}:{suffix}"

    @staticmethod
    def _render_json(value: object) -> str:
        return f"```json\n{json.dumps(value, ensure_ascii=True, indent=2, default=str)[:12000]}\n```"

    @classmethod
    def _render_models(cls, payload: object) -> str:
        if not isinstance(payload, dict):
            return cls._render_json(payload)
        models = payload.get("data")
        if not isinstance(models, list):
            return cls._render_json(payload)
        lines = ["**Native models**"]
        for item in models[:100]:
            if isinstance(item, dict):
                identifier = item.get("id") or item.get("modelId") or item.get("slug")
                label = item.get("displayName") or identifier
                if identifier:
                    lines.append(f"- `{identifier}` — {label}")
        return "\n".join(lines) if len(lines) > 1 else "No native models were returned."

    @staticmethod
    def _render_config_value(payload: object, *keys: str) -> str:
        config = payload.get("config") if isinstance(payload, dict) else None
        config = config if isinstance(config, dict) else payload if isinstance(payload, dict) else {}
        for key in keys:
            if key in config:
                return f"`{key}`: `{config[key]}`"
        return "No value is configured."

    @staticmethod
    def _config_value(payload: object, *keys: str) -> str:
        config = payload.get("config") if isinstance(payload, dict) else None
        config = config if isinstance(config, dict) else payload if isinstance(payload, dict) else {}
        for key in keys:
            if key not in config:
                continue
            value = config[key]
            if isinstance(value, dict):
                value = value.get("mode") or value.get("type")
            if value is not None:
                return str(value).strip()
        return ""

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        detail = " ".join(str(exc).split())[:500]
        return f"**Error:** {detail or type(exc).__name__}"

    @staticmethod
    def _reply(message: InboundMessage, text: str) -> OutboundMessage:
        return OutboundMessage(
            delivery_id=f"imcodex:controller:{message.message_id}:error",
            conversation_ref=message.conversation_ref,
            content=(TextContent(text, TextFormat.MARKDOWN),),
            created_at=datetime.now(UTC),
            reply_to=message.message_id,
            metadata={"imcodex_product_controller": True},
        )

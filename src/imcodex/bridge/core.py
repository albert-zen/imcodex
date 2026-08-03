from __future__ import annotations

import uuid

from ..appserver import (
    AppServerError,
    StaleThreadBindingError,
    ThreadSelectionError,
)
from ..models import InboundMessage, OutboundMessage
from ..observability.message_trace import ensure_trace_id, text_preview, text_sha256
from ..observability.runtime import emit_event
from .rendering import BridgeRenderingMixin
from .settings import (
    rate_limit_reset_credit_items,
    render_credits,
    render_fast_mode,
    render_models,
    render_native_config_write_result,
    render_permission_modes,
    render_permission_set_result,
    render_personality,
    render_rate_limit_reset_result,
    render_reasoning_effort,
)
from .thread_handoff import ThreadHandoffMixin
from .thread_views import ThreadViewMixin

_SYSTEM_MESSAGE_TYPES = frozenset({"accepted", "status", "error"})
_SYSTEM_PREFIX = "[System] "
_INPUT_ERROR_TEXT = {
    "image_too_large": (
        "Images must be JPEG, PNG, or WebP, at most 10 MiB, and no more than 40 megapixels."
    ),
    "too_many_images": "You can send up to 4 images in one message.",
    "unsupported_image": "Supported image formats are JPEG, PNG, and WebP.",
    "invalid_image": "That image appears to be damaged or incomplete. Please resend it.",
    "image_download_failed": "I couldn't download that image. Please resend it.",
    "file_too_large": "Files must be at most 25 MiB.",
    "too_many_files": "You can send up to 4 files in one message.",
    "unsupported_file": (
        "Supported files are PDF, plain text, Markdown, and common source-code formats."
    ),
    "invalid_file": "That file appears damaged, binary, or inconsistent with its filename.",
    "file_download_failed": "I couldn't download that file. Please resend it.",
}
_GENERIC_ATTACHMENT_ERROR_TEXT = "I couldn't process that attachment. Please resend it."
_REMOTE_APP_SERVER_ATTACHMENT_ERROR_TEXT = (
    "Attachment input requires imcodex and Codex App Server to share a verified local filesystem. "
    "Use the IMCodex-managed local App Server or a same-filesystem stdio/Unix endpoint, then resend the attachment."
)


class ImcodexCommandPolicy(
    ThreadHandoffMixin,
    ThreadViewMixin,
    BridgeRenderingMixin,
):
    def __init__(
        self,
        *,
        store,
        backend,
        command_router,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router

    async def close(self) -> None:
        return None

    def preflight_inbound_attachments(
        self,
        message: InboundMessage,
    ) -> list[OutboundMessage] | None:
        """Return a terminal response before a channel stages local media."""

        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None:
            return [self._message(message, "status", self._render_onboarding())]
        if not self._supports_local_image_paths():
            return [
                self._message(
                    message, "error", _REMOTE_APP_SERVER_ATTACHMENT_ERROR_TEXT
                )
            ]
        return None

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        trace_id = ensure_trace_id(message)
        if message.input_error is not None:
            message_kind = "input_error"
        elif message.attachments:
            message_kind = "multimodal" if message.text.strip() else "attachment"
        elif message.text.startswith("/"):
            message_kind = "command"
        else:
            message_kind = "text"
        emit_event(
            component="bridge",
            event="bridge.inbound.started",
            message="Bridge started handling inbound message",
            trace_id=trace_id,
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            message_id=message.message_id,
            data={
                "message_kind": message_kind,
                "text_length": len(message.text),
                "text_preview": text_preview(message.text),
                "text_sha256": text_sha256(message.text),
                "attachment_count": len(message.attachments),
                "attachment_kinds": [
                    attachment.kind for attachment in message.attachments
                ],
                "has_quote": message.quote is not None,
                "quoted_attachment_count": (
                    len(message.quote.attachments) if message.quote is not None else 0
                ),
            },
        )
        try:
            if message.input_error is not None:
                outbound = [
                    self._message(
                        message,
                        "error",
                        _INPUT_ERROR_TEXT.get(
                            message.input_error, _GENERIC_ATTACHMENT_ERROR_TEXT
                        ),
                    )
                ]
            elif message_kind == "command":
                outbound = await self._handle_command(message)
            else:
                outbound = await self._handle_input(message)
        except Exception as exc:
            emit_event(
                component="bridge",
                event="bridge.inbound.failed",
                level="ERROR",
                message="Bridge failed while handling inbound message",
                trace_id=trace_id,
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_id=message.message_id,
                data={"error_type": type(exc).__name__},
            )
            raise
        emit_event(
            component="bridge",
            event="bridge.inbound.completed",
            message="Bridge finished handling inbound message",
            trace_id=trace_id,
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            message_id=message.message_id,
            data={
                "message_kind": message_kind,
                "outbound_count": len(outbound),
                "outbound_message_types": [item.message_type for item in outbound],
            },
        )
        return outbound

    async def _handle_input(self, message: InboundMessage) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None:
            return [self._message(message, "status", self._render_onboarding())]
        # Bound non-command input is dispatched by the SDK Gateway/Application.
        return []

    def _supports_local_image_paths(self) -> bool:
        capability = getattr(self.backend, "supports_local_image_paths", None)
        if callable(capability):
            return bool(capability())
        if capability is not None:
            return bool(capability)
        return False

    def _stale_thread_status(
        self, message: InboundMessage, exc: StaleThreadBindingError
    ) -> str:
        self.store.clear_thread_binding(message.channel_id, message.conversation_id)
        return (
            f"Current thread {exc.thread_id} could not be resumed. "
            "Use /threads to pick another thread or /new to start fresh."
        )

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        response = self.command_router.handle(
            message.channel_id, message.conversation_id, message.text
        )
        if response.action == "threads.query":
            try:
                payload = response.payload or {}
                text = await self._render_threads(
                    message,
                    page=int(payload.get("page") or 1),
                    query=str(payload.get("query") or "").strip() or None,
                    project=str(payload.get("project") or "").strip() or None,
                    refresh=bool(payload.get("refresh", True)),
                )
            except AppServerError:
                text = (
                    "Threads could not be refreshed from Codex right now. "
                    "Use /status, /thread read, or try /threads again in a moment."
                )
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", text)]
        if response.action == "status.query":
            try:
                text = await self._render_status(message)
            except AppServerError as exc:
                binding = self.store.get_binding(
                    message.channel_id, message.conversation_id
                )
                text = (
                    f"Current thread {binding.thread_id} could not be queried from Codex right now: {self._safe_appserver_error(exc)}. "
                    "Try again in a moment."
                )
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", text)]
        if response.action == "models.list":
            result = await self.backend.list_models()
            return [self._message(message, "command_result", render_models(result))]
        if response.action == "settings.permission.read":
            try:
                result = await self.backend.read_permission_options(
                    message.channel_id, message.conversation_id
                )
            except AppServerError as exc:
                text = f"Permission modes could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [
                self._message(
                    message, "command_result", render_permission_modes(result)
                )
            ]
        if response.action == "settings.reasoning.read":
            try:
                result = await self.backend.read_reasoning_options(
                    message.channel_id, message.conversation_id
                )
            except AppServerError as exc:
                text = f"Reasoning effort could not be queried from Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [
                self._message(
                    message, "command_result", render_reasoning_effort(result)
                )
            ]
        if response.action == "settings.personality.read":
            try:
                result = await self.backend.read_personality_options(
                    message.channel_id,
                    message.conversation_id,
                )
            except AppServerError as exc:
                text = f"Personality could not be queried from Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [
                self._message(message, "command_result", render_personality(result))
            ]
        if response.action == "settings.fast.read":
            result = await self.backend.read_fast_options(
                message.channel_id, message.conversation_id
            )
            return [self._message(message, "command_result", render_fast_mode(result))]
        if response.action == "credits.read":
            try:
                result = await self.backend.read_account_credits()
            except AppServerError as exc:
                text = f"Credits could not be queried from Codex right now: {self._safe_appserver_error(exc)}. Try again in a moment."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_credits(result))]
        if response.action == "credits.reset":
            selector = str(
                (response.payload or {}).get("credit_selector") or ""
            ).strip()
            credit_id = None
            if selector.isdigit():
                selected_index = int(selector)
                if selected_index < 1:
                    return [
                        self._message(
                            message,
                            "status",
                            "Reset number must be 1 or greater. Run /credits to see available resets.",
                        )
                    ]
                try:
                    rate_limits = await self.backend.read_account_rate_limits()
                except AppServerError as exc:
                    text = (
                        "Available resets could not be queried from Codex right now: "
                        f"{self._safe_appserver_error(exc)}."
                    )
                    return [self._message(message, "status", text)]
                reset_items = rate_limit_reset_credit_items(rate_limits)
                if selected_index > len(reset_items):
                    return [
                        self._message(
                            message,
                            "status",
                            f"Reset {selected_index} is not in the current Codex snapshot. "
                            "Run /credits and choose one of the listed numbers.",
                        )
                    ]
                credit_id = str(reset_items[selected_index - 1].get("id") or "").strip()
                if not credit_id:
                    return [
                        self._message(
                            message,
                            "status",
                            f"Reset {selected_index} has no selectable ID in the current Codex snapshot.",
                        )
                    ]
            elif selector:
                credit_id = selector
            try:
                result = await self.backend.consume_account_rate_limit_reset_credit(
                    idempotency_key=self._rate_limit_reset_idempotency_key(message),
                    credit_id=credit_id,
                )
            except AppServerError as exc:
                text = (
                    "Rate-limit reset could not be used right now: "
                    f"{self._safe_appserver_error(exc)}."
                )
                return [self._message(message, "status", text)]
            refreshed = None
            refresh_failed = False
            try:
                refreshed = await self.backend.read_account_credits()
            except AppServerError:
                refresh_failed = True
            return [
                self._message(
                    message,
                    "command_result",
                    render_rate_limit_reset_result(
                        result,
                        refreshed=refreshed,
                        refresh_failed=refresh_failed,
                    ),
                )
            ]
        if response.action == "native.events":
            return [
                self._message(
                    message,
                    "command_result",
                    "Native event inspection is unavailable: the SDK does not expose raw "
                    "Application events to consumers.",
                )
            ]
        if response.action == "goal.read":
            try:
                result = await self.backend.read_thread_goal(
                    message.channel_id, message.conversation_id
                )
            except StaleThreadBindingError as exc:
                return [
                    self._message(
                        message, "status", self._stale_thread_status(message, exc)
                    )
                ]
            except AppServerError as exc:
                text = f"Goal could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", self._render_goal(result))]
        if response.action == "goal.set":
            payload = response.payload or {}
            try:
                result = await self.backend.set_thread_goal(
                    message.channel_id,
                    message.conversation_id,
                    objective=str(payload.get("objective") or ""),
                    status="active",
                )
            except KeyError:
                return [
                    self._message(
                        message, "status", "Choose a CWD first with /cwd <path>."
                    )
                ]
            except StaleThreadBindingError as exc:
                return [
                    self._message(
                        message, "status", self._stale_thread_status(message, exc)
                    )
                ]
            except AppServerError as exc:
                text = f"Goal could not be set in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "status", self._render_goal(result))]
        if response.action == "goal.status":
            binding = self.store.get_binding(
                message.channel_id, message.conversation_id
            )
            if binding.thread_id is None:
                return [
                    self._message(message, "command_result", "No goal currently set.")
                ]
            payload = response.payload or {}
            try:
                result = await self.backend.set_thread_goal(
                    message.channel_id,
                    message.conversation_id,
                    status=str(payload.get("status") or ""),
                )
            except StaleThreadBindingError as exc:
                return [
                    self._message(
                        message, "status", self._stale_thread_status(message, exc)
                    )
                ]
            except AppServerError as exc:
                text = f"Goal could not be updated in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "status", self._render_goal(result))]
        if response.action == "goal.clear":
            try:
                result = await self.backend.clear_thread_goal(
                    message.channel_id, message.conversation_id
                )
            except StaleThreadBindingError as exc:
                return [
                    self._message(
                        message, "status", self._stale_thread_status(message, exc)
                    )
                ]
            except AppServerError as exc:
                text = f"Goal could not be cleared in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = (
                "Goal cleared." if result.get("cleared") else "No goal currently set."
            )
            return [self._message(message, "status", text)]
        if response.action == "config.read":
            result = await self.backend.read_config(
                message.channel_id, message.conversation_id
            )
            key_path = (
                None if response.payload is None else response.payload.get("key_path")
            )
            return [
                self._message(
                    message, "command_result", self._render_config(result, key_path)
                )
            ]
        if response.action == "config.write":
            payload = response.payload or {}
            await self.backend.write_config_value(
                key_path=str(payload.get("key_path") or ""),
                value=payload.get("value"),
            )
            return [self._message(message, "status", response.text)]
        if response.action == "config.batch":
            payload = response.payload or {}
            await self.backend.batch_write_config(
                edits=list(payload.get("edits") or []),
                reload_user_config=bool(payload.get("reload_user_config", False)),
            )
            return [self._message(message, "status", response.text)]
        if response.action == "settings.model":
            try:
                result = await self.backend.set_model(
                    message.channel_id,
                    message.conversation_id,
                    None if response.payload is None else response.payload.get("model"),
                )
            except AppServerError as exc:
                text = f"Model could not be set in Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = render_native_config_write_result(
                result, response.text, setting_label="Model"
            )
            return [self._message(message, "status", text)]
        if response.action == "settings.reasoning.write":
            payload = response.payload or {}
            try:
                result = await self.backend.set_reasoning_effort(
                    message.channel_id,
                    message.conversation_id,
                    payload.get("effort"),
                )
            except AppServerError as exc:
                text = f"Reasoning effort could not be set in Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = render_native_config_write_result(
                result, response.text, setting_label="Reasoning effort"
            )
            return [self._message(message, "status", text)]
        if response.action == "settings.personality.write":
            payload = response.payload or {}
            try:
                result = await self.backend.set_personality(
                    message.channel_id,
                    message.conversation_id,
                    payload.get("personality"),
                )
            except AppServerError as exc:
                text = f"Personality could not be set in Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = render_native_config_write_result(
                result, response.text, setting_label="Personality"
            )
            return [self._message(message, "status", text)]
        if response.action == "settings.fast.write":
            payload = response.payload or {}
            try:
                result = await self.backend.set_fast_mode(
                    message.channel_id,
                    message.conversation_id,
                    payload.get("enabled") is True,
                )
            except AppServerError as exc:
                text = f"Fast mode could not be set in Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = render_native_config_write_result(
                result, response.text, setting_label="Fast mode"
            )
            return [self._message(message, "status", text)]
        if response.action == "settings.permission.write":
            payload = response.payload or {}
            try:
                result = await self.backend.set_permission_mode(
                    message.channel_id,
                    message.conversation_id,
                    str(payload.get("mode") or ""),
                )
            except AppServerError as exc:
                text = f"Permission mode could not be set in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [
                self._message(message, "status", render_permission_set_result(result))
            ]
        if response.action == "thread.read.query":
            return [
                self._message(
                    message,
                    "command_result",
                    await self._render_thread(message, response.thread_id),
                )
            ]
        if response.action == "thread.history.query":
            return await self._handle_thread_history_command(
                message,
                limit=int((response.payload or {}).get("limit") or 1),
                page=int((response.payload or {}).get("page") or 1),
            )
        if response.action == "thread.catchup.query":
            return await self._handle_thread_catchup_command(
                message,
                limit=int((response.payload or {}).get("limit") or 5),
            )
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(
                message.channel_id, message.conversation_id
            )
            return [self._message(message, "status", f"Started thread {thread_id}.")]
        if response.action == "thread.fork":
            try:
                snapshot = await self.backend.fork_thread(
                    message.channel_id, message.conversation_id
                )
            except AppServerError as exc:
                return [
                    self._message(
                        message,
                        "status",
                        f"Thread could not be forked: {self._safe_appserver_error(exc)}.",
                    )
                ]
            if snapshot.cwd:
                return [
                    self._message(
                        message,
                        "status",
                        f"Forked to {self._thread_label(snapshot)}.\nCWD: {snapshot.cwd}",
                    )
                ]
            return [
                self._message(
                    message, "status", f"Forked to {self._thread_label(snapshot)}."
                )
            ]
        if response.action == "thread.rename":
            name = str((response.payload or {}).get("name") or "").strip()
            try:
                await self.backend.rename_thread(
                    message.channel_id, message.conversation_id, name
                )
            except AppServerError as exc:
                return [
                    self._message(
                        message,
                        "status",
                        f"Thread could not be renamed: {self._safe_appserver_error(exc)}.",
                    )
                ]
            return [self._message(message, "status", f"Renamed thread to {name}.")]
        if response.action == "thread.compact":
            try:
                await self.backend.compact_thread(
                    message.channel_id, message.conversation_id
                )
            except AppServerError as exc:
                return [
                    self._message(
                        message,
                        "status",
                        f"Compaction could not be started: {self._safe_appserver_error(exc)}.",
                    )
                ]
            return [self._message(message, "status", "Compaction started.")]
        if response.action == "thread.pick":
            context = self.store.get_thread_browser_context(
                message.channel_id, message.conversation_id
            )
            if context is None:
                return [self._message(message, "error", "Use /threads first.")]
            index = int((response.payload or {}).get("index") or 0)
            if index < 0 or index >= len(context.thread_ids):
                return [
                    self._message(
                        message, "error", "Pick a number from the current page."
                    )
                ]
            thread_id = context.thread_ids[index]
            return await self._switch_thread(
                message,
                thread_id,
                history_limit=(response.payload or {}).get("history_limit"),
                catchup_limit=(response.payload or {}).get("catchup_limit"),
                verb="Switched to",
            )
        if response.action == "thread.pick.query":
            payload = response.payload or {}
            return await self._handle_direct_thread_pick(
                message,
                query=str(payload.get("query") or "").strip(),
                history_limit=payload.get("history_limit"),
                catchup_limit=payload.get("catchup_limit"),
            )
        if response.action == "threads.exit":
            self.store.clear_thread_browser_context(
                message.channel_id, message.conversation_id
            )
            return [self._message(message, "status", response.text)]
        if response.action == "thread.attach":
            try:
                selector = str(
                    (response.payload or {}).get("selector") or response.thread_id or ""
                ).strip()
                snapshot = await self.backend.resolve_thread_selector(
                    message.channel_id,
                    message.conversation_id,
                    selector,
                )
            except ThreadSelectionError as exc:
                return [self._message(message, "error", str(exc))]
            except Exception as exc:
                return [
                    self._message(
                        message,
                        "status",
                        f"Thread could not be attached: {self._safe_exception_text(exc)}.",
                    )
                ]
            return await self._switch_thread(
                message,
                snapshot.thread_id,
                history_limit=None,
                catchup_limit=None,
                verb="Attached to",
            )
        if response.action == "turn.stop":
            interrupted = await self.backend.interrupt_active_turn(
                message.channel_id,
                message.conversation_id,
            )
            if not interrupted:
                return [
                    self._message(message, "command_result", "No active turn to stop.")
                ]
        if response.action in {
            "approval.accept",
            "approval.deny",
            "approval.cancel",
            "request.answer",
        }:
            return [
                self._message(
                    message,
                    "status",
                    "Interactive requests are routed by the SDK request presenter.",
                    request_id=response.request_id,
                )
            ]
        if response.action == "native.call":
            payload = response.payload or {}
            result = await self.backend.call_native(
                str(payload.get("method") or ""),
                payload.get("params")
                if isinstance(payload.get("params"), dict)
                else {},
            )
            return [self._message(message, "command_result", self._render_json(result))]
        message_type = self._command_message_type(response.action)
        return [
            self._message(
                message, message_type, response.text, request_id=response.request_id
            )
        ]

    def _rate_limit_reset_idempotency_key(inbound: InboundMessage) -> str:
        stable_id = str(inbound.message_id or inbound.trace_id or "").strip()
        if not stable_id:
            return str(uuid.uuid4())
        identity = "\0".join(
            (inbound.channel_id, inbound.conversation_id, stable_id, "credits.reset")
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    def _message(
        self,
        inbound: InboundMessage,
        message_type: str,
        text: str,
        *,
        request_id: str | None = None,
    ) -> OutboundMessage:
        if message_type in _SYSTEM_MESSAGE_TYPES and not text.startswith(
            _SYSTEM_PREFIX
        ):
            text = f"{_SYSTEM_PREFIX}{text}"
        return OutboundMessage(
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            message_type=message_type,
            text=text,
            request_id=request_id,
            metadata={"trace_id": inbound.trace_id} if inbound.trace_id else {},
        )

    def _command_message_type(self, action: str) -> str:
        if action in {
            "project.cwd",
            "settings.view",
            "settings.visibility",
            "settings.model",
            "settings.reasoning.write",
            "settings.personality.write",
            "settings.fast.write",
            "settings.permission.write",
            "goal.set",
            "goal.status",
            "goal.clear",
            "config.write",
            "config.batch",
            "threads.exit",
            "approval.accept",
            "approval.deny",
            "approval.cancel",
        }:
            return "status"
        if (
            action.endswith(".invalid")
            or action.endswith(".missing")
            or ".missing" in action
            or action == "unknown"
        ):
            return "error"
        if action in {"thread.read.none", "turn.stop.none"}:
            return "command_result"
        return "command_result"

    def _render_onboarding(self) -> str:
        return "\n".join(
            [
                "Before we start, I need a working folder.",
                "",
                "Use /cwd playground for a default workspace.",
                "Use /cwd <path> to point me at an existing folder.",
            ]
        )

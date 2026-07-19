from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from ..appserver import (
    AppServerError,
    StaleThreadBindingError,
    ThreadSelectionError,
    normalize_appserver_message,
)
from ..models import InboundMessage, OutboundMessage
from ..observability.message_trace import ensure_trace_id, text_preview, text_sha256
from ..observability.runtime import emit_event
from .inbound import render_inbound_input
from .native_events import (
    clamp_native_events_limit,
    record_native_appserver_journal,
    render_native_events,
    select_native_events,
)
from .rendering import BridgeRenderingMixin
from .server_requests import NativeRequestPolicy
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
from .terminal_delivery import TerminalDeliveryMixin
from .thread_handoff import ThreadHandoffMixin
from .thread_views import ThreadViewMixin


_SYSTEM_MESSAGE_TYPES = frozenset({"accepted", "status", "error"})
_SYSTEM_PREFIX = "[System] "
_SERVER_REQUEST_DELIVERY_TIMEOUT_S = 10.0
_SERVER_REQUEST_DELIVERY_RETRY_DELAYS_S = (0.1, 0.5, 1.0, 2.0)
_RECOVERY_DELIVERY_TIMEOUT_S = 10.0
_DELIVERY_FAILED_REQUEST_CODE = -32603
_RECENT_TERMINAL_DELIVERY_LIMIT = 512
_TERMINAL_PROJECTION_EVENT_KINDS = frozenset({"item_completed", "turn_completed"})
_INPUT_ERROR_TEXT = {
    "image_too_large": (
        "Images must be JPEG, PNG, or WebP, at most 10 MiB, and no more than 40 megapixels."
    ),
    "too_many_images": "You can send up to 4 images in one message.",
    "unsupported_image": "Supported image formats are JPEG, PNG, and WebP.",
    "invalid_image": "That image appears to be damaged or incomplete. Please resend it.",
    "image_download_failed": "I couldn't download that image. Please resend it.",
}
_GENERIC_ATTACHMENT_ERROR_TEXT = "I couldn't process that attachment. Please resend it."
_REMOTE_APP_SERVER_IMAGE_ERROR_TEXT = (
    "Image input requires imcodex and Codex App Server to share a verified local filesystem. "
    "Use the IMCodex-managed local App Server or a same-filesystem stdio/Unix endpoint, then resend the image."
)


class BridgeService(
    TerminalDeliveryMixin,
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
        projector,
        outbound_sink=None,
        server_request_delivery_timeout_s: float = _SERVER_REQUEST_DELIVERY_TIMEOUT_S,
        native_thread_tool_host: bool = False,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router
        self.projector = projector
        self.outbound_sink = outbound_sink
        self.server_request_delivery_timeout_s = max(
            0.01,
            float(server_request_delivery_timeout_s),
        )
        self._rehydration_lock = asyncio.Lock()
        self._terminal_projection_lock = asyncio.Lock()
        self._init_thread_handoff()
        # These are short-lived presentation facts, not native turn state. The
        # pending map keeps a recovered result retryable until an IM sink
        # confirms delivery; the bounded delivered map closes the race between
        # queued terminal notifications and a concurrent resume response.
        self._pending_recovered_turns: dict[tuple[str, str], dict] = {}
        self._recent_terminal_deliveries: dict[tuple[str, str], None] = {}
        self._init_terminal_delivery()
        self.native_requests = NativeRequestPolicy(
            store=store,
            backend=backend,
            native_thread_tool_host=native_thread_tool_host,
        )

    async def close(self) -> None:
        await self._close_terminal_delivery()
        await self._close_thread_handoff()
        await self.native_requests.close()

    def preflight_inbound_attachments(
        self,
        message: InboundMessage,
    ) -> list[OutboundMessage] | None:
        """Return a terminal response before a channel stages local media."""

        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None:
            return [self._message(message, "status", self._render_onboarding())]
        if not self._supports_local_image_paths():
            return [self._message(message, "error", _REMOTE_APP_SERVER_IMAGE_ERROR_TEXT)]
        return None

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        trace_id = ensure_trace_id(message)
        if message.input_error is not None:
            message_kind = "input_error"
        elif message.attachments:
            message_kind = "multimodal" if message.text.strip() else "image"
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
                "attachment_kinds": [attachment.kind for attachment in message.attachments],
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
                        _INPUT_ERROR_TEXT.get(message.input_error, _GENERIC_ATTACHMENT_ERROR_TEXT),
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
        if message.attachments and not self._supports_local_image_paths():
            return [self._message(message, "error", _REMOTE_APP_SERVER_IMAGE_ERROR_TEXT)]
        pending_approvals = self.store.list_pending_requests(
            message.channel_id,
            message.conversation_id,
            kind="approval",
        )
        if pending_approvals:
            failure = await self._resolve_projected_requests(
                inbound=message,
                request_ids=[route.request_id for route in pending_approvals],
                decision="cancel",
                action="approval.cancel",
                continue_on_failure=False,
            )
            if failure is not None:
                return failure
        try:
            submission = await self.backend.submit_input(
                message.channel_id,
                message.conversation_id,
                render_inbound_input(message),
                message.attachments,
            )
        except StaleThreadBindingError as exc:
            self.store.clear_thread_binding(message.channel_id, message.conversation_id)
            text = (
                f"Current thread {exc.thread_id} could not be resumed. "
                "Use /threads to pick another thread or /new to start fresh."
            )
            return [self._message(message, "status", text)]
        if submission.kind == "start":
            self.store.note_active_turn(submission.thread_id, submission.turn_id, "inProgress")
        return []

    def _supports_local_image_paths(self) -> bool:
        capability = getattr(self.backend, "supports_local_image_paths", None)
        if callable(capability):
            return bool(capability())
        if capability is not None:
            return bool(capability)
        return False

    def _stale_thread_status(self, message: InboundMessage, exc: StaleThreadBindingError) -> str:
        self.store.clear_thread_binding(message.channel_id, message.conversation_id)
        return (
            f"Current thread {exc.thread_id} could not be resumed. "
            "Use /threads to pick another thread or /new to start fresh."
        )

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        response = self.command_router.handle(message.channel_id, message.conversation_id, message.text)
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
                binding = self.store.get_binding(message.channel_id, message.conversation_id)
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
                result = await self.backend.read_permission_options(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                text = f"Permission modes could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_permission_modes(result))]
        if response.action == "settings.reasoning.read":
            try:
                result = await self.backend.read_reasoning_options(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                text = f"Reasoning effort could not be queried from Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_reasoning_effort(result))]
        if response.action == "settings.personality.read":
            try:
                result = await self.backend.read_personality_options(
                    message.channel_id,
                    message.conversation_id,
                )
            except AppServerError as exc:
                text = f"Personality could not be queried from Codex: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_personality(result))]
        if response.action == "settings.fast.read":
            result = await self.backend.read_fast_options(message.channel_id, message.conversation_id)
            return [self._message(message, "command_result", render_fast_mode(result))]
        if response.action == "credits.read":
            try:
                result = await self.backend.read_account_credits()
            except AppServerError as exc:
                text = f"Credits could not be queried from Codex right now: {self._safe_appserver_error(exc)}. Try again in a moment."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_credits(result))]
        if response.action == "credits.reset":
            selector = str((response.payload or {}).get("credit_selector") or "").strip()
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
            payload = response.payload or {}
            filters = [str(token) for token in list(payload.get("filters") or [])]
            limit = clamp_native_events_limit(payload.get("limit"))
            entries = select_native_events(self.store, limit=limit, filters=filters)
            return [self._message(message, "command_result", render_native_events(entries, filters=filters))]
        if response.action == "goal.read":
            try:
                result = await self.backend.read_thread_goal(message.channel_id, message.conversation_id)
            except StaleThreadBindingError as exc:
                return [self._message(message, "status", self._stale_thread_status(message, exc))]
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
                return [self._message(message, "status", "Choose a CWD first with /cwd <path>.")]
            except StaleThreadBindingError as exc:
                return [self._message(message, "status", self._stale_thread_status(message, exc))]
            except AppServerError as exc:
                text = f"Goal could not be set in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "status", self._render_goal(result))]
        if response.action == "goal.status":
            binding = self.store.get_binding(message.channel_id, message.conversation_id)
            if binding.thread_id is None:
                return [self._message(message, "command_result", "No goal currently set.")]
            payload = response.payload or {}
            try:
                result = await self.backend.set_thread_goal(
                    message.channel_id,
                    message.conversation_id,
                    status=str(payload.get("status") or ""),
                )
            except StaleThreadBindingError as exc:
                return [self._message(message, "status", self._stale_thread_status(message, exc))]
            except AppServerError as exc:
                text = f"Goal could not be updated in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            return [self._message(message, "status", self._render_goal(result))]
        if response.action == "goal.clear":
            try:
                result = await self.backend.clear_thread_goal(message.channel_id, message.conversation_id)
            except StaleThreadBindingError as exc:
                return [self._message(message, "status", self._stale_thread_status(message, exc))]
            except AppServerError as exc:
                text = f"Goal could not be cleared in Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "status", text)]
            text = "Goal cleared." if result.get("cleared") else "No goal currently set."
            return [self._message(message, "status", text)]
        if response.action == "config.read":
            result = await self.backend.read_config(message.channel_id, message.conversation_id)
            key_path = None if response.payload is None else response.payload.get("key_path")
            return [self._message(message, "command_result", self._render_config(result, key_path))]
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
            text = render_native_config_write_result(result, response.text, setting_label="Model")
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
            text = render_native_config_write_result(result, response.text, setting_label="Reasoning effort")
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
            text = render_native_config_write_result(result, response.text, setting_label="Personality")
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
            text = render_native_config_write_result(result, response.text, setting_label="Fast mode")
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
            return [self._message(message, "status", render_permission_set_result(result))]
        if response.action == "thread.read.query":
            return [self._message(message, "command_result", await self._render_thread(message, response.thread_id))]
        if response.action == "thread.history.query":
            return await self._handle_thread_history_command(
                message,
                limit=int((response.payload or {}).get("limit") or 1),
            )
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            return [self._message(message, "status", f"Started thread {thread_id}.")]
        if response.action == "thread.fork":
            try:
                snapshot = await self.backend.fork_thread(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                return [
                    self._message(message, "status", f"Thread could not be forked: {self._safe_appserver_error(exc)}.")
                ]
            if snapshot.cwd:
                return [
                    self._message(message, "status", f"Forked to {self._thread_label(snapshot)}.\nCWD: {snapshot.cwd}")
                ]
            return [self._message(message, "status", f"Forked to {self._thread_label(snapshot)}.")]
        if response.action == "thread.rename":
            name = str((response.payload or {}).get("name") or "").strip()
            try:
                await self.backend.rename_thread(message.channel_id, message.conversation_id, name)
            except AppServerError as exc:
                return [
                    self._message(message, "status", f"Thread could not be renamed: {self._safe_appserver_error(exc)}.")
                ]
            return [self._message(message, "status", f"Renamed thread to {name}.")]
        if response.action == "thread.compact":
            try:
                await self.backend.compact_thread(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                return [
                    self._message(
                        message, "status", f"Compaction could not be started: {self._safe_appserver_error(exc)}."
                    )
                ]
            return [self._message(message, "status", "Compaction started.")]
        if response.action == "thread.pick":
            context = self.store.get_thread_browser_context(message.channel_id, message.conversation_id)
            if context is None:
                return [self._message(message, "error", "Use /threads first.")]
            index = int((response.payload or {}).get("index") or 0)
            if index < 0 or index >= len(context.thread_ids):
                return [self._message(message, "error", "Pick a number from the current page.")]
            thread_id = context.thread_ids[index]
            return await self._switch_thread(
                message,
                thread_id,
                history_limit=(response.payload or {}).get("history_limit"),
                verb="Switched to",
            )
        if response.action == "threads.exit":
            self.store.clear_thread_browser_context(message.channel_id, message.conversation_id)
            return [self._message(message, "status", response.text)]
        if response.action == "thread.attach":
            try:
                selector = str((response.payload or {}).get("selector") or response.thread_id or "").strip()
                snapshot = await self.backend.resolve_thread_selector(
                    message.channel_id,
                    message.conversation_id,
                    selector,
                )
            except ThreadSelectionError as exc:
                return [self._message(message, "error", str(exc))]
            except Exception as exc:
                return [
                    self._message(message, "status", f"Thread could not be attached: {self._safe_exception_text(exc)}.")
                ]
            return await self._switch_thread(
                message,
                snapshot.thread_id,
                history_limit=None,
                verb="Attached to",
            )
        if response.action == "turn.stop":
            interrupted = await self.backend.interrupt_active_turn(message.channel_id, message.conversation_id)
            if not interrupted:
                return [self._message(message, "command_result", "No active turn to stop.")]
        elif response.action in {"approval.accept", "approval.deny", "approval.cancel"}:
            decision = {
                "approval.accept": "accept",
                "approval.deny": "decline",
                "approval.cancel": "cancel",
            }[response.action]
            failure = await self._resolve_projected_requests(
                inbound=message,
                request_ids=response.request_ids or ([response.request_id] if response.request_id else []),
                decision=decision,
                action=response.action,
                continue_on_failure=False,
            )
            if failure is not None:
                return failure
        elif response.action == "request.answer":
            payload = {"answers": {key: {"answers": value} for key, value in (response.answers or {}).items()}}
            try:
                await self.backend.reply_to_server_request(response.request_id or "", payload)
            except (AppServerError, KeyError) as exc:
                return await self._request_reply_failure(message, response.request_id, response.action, exc)
        elif response.action == "native.call":
            payload = response.payload or {}
            result = await self.backend.call_native(
                str(payload.get("method") or ""),
                payload.get("params") if isinstance(payload.get("params"), dict) else {},
            )
            return [self._message(message, "command_result", self._render_json(result))]
        elif response.action == "native.respond":
            try:
                await self.backend.reply_to_server_request(response.request_id or "", response.payload or {})
            except (AppServerError, KeyError) as exc:
                return await self._request_reply_failure(message, response.request_id, response.action, exc)
        elif response.action == "native.error":
            payload = response.payload or {}
            try:
                await self.backend.reply_error_to_server_request(
                    response.request_id or "",
                    code=int(payload.get("code") or 0),
                    message=str(payload.get("message") or ""),
                    data=payload.get("data"),
                )
            except (AppServerError, KeyError) as exc:
                return await self._request_reply_failure(message, response.request_id, response.action, exc)
        message_type = self._command_message_type(response.action)
        return [self._message(message, message_type, response.text, request_id=response.request_id)]

    async def handle_notification(self, notification: dict) -> list[OutboundMessage]:
        self.native_requests.observe_notification(notification)
        journal_entry = record_native_appserver_journal(self.store, notification)
        gate_outcome = await self._buffer_thread_output(
            kind="notification",
            payload=notification,
            journal_sequence=journal_entry.sequence,
        )
        if gate_outcome is not None:
            self.store.update_native_appserver_event(
                journal_entry.sequence,
                outcome="buffered",
                note="waiting for thread switch response delivery",
            )
            return []
        return await self._process_notification(notification, journal_entry.sequence)

    async def _process_notification(
        self,
        notification: dict,
        journal_sequence: int,
        *,
        replay_message: OutboundMessage | None = None,
        replay_prepared: bool = False,
        capture_projection=None,
    ) -> list[OutboundMessage]:
        event = normalize_appserver_message(notification)
        if event.kind in _TERMINAL_PROJECTION_EVENT_KINDS:
            async with self._terminal_projection_lock:
                event_terminal_key = self._event_terminal_key(event)
                staged_terminal = self._terminal_delivery_is_staged(event_terminal_key)
                if staged_terminal and event_terminal_key is not None:
                    if not replay_prepared:
                        self.projector.project_notification(notification, self.store)
                    self.projector.discard_recovered_turn(
                        thread_id=event_terminal_key[0],
                        turn_id=event_terminal_key[1],
                    )
                    message = None
                else:
                    message = (
                        replay_message
                        if replay_prepared
                        else self.projector.project_notification(notification, self.store)
                    )
                terminal_key = self._terminal_delivery_key(event, message)
                if terminal_key is not None and terminal_key in self._recent_terminal_deliveries:
                    self.projector.discard_recovered_turn(
                        thread_id=terminal_key[0],
                        turn_id=terminal_key[1],
                    )
                    message = None
                if not replay_prepared:
                    self._attach_delivery_id(
                        message,
                        notification,
                        namespace="projection",
                        terminal_key=terminal_key,
                    )
                    if callable(capture_projection):
                        capture_projection(message)
                if terminal_key is not None and message is not None:
                    outbound, delivered = await self._deliver_terminal_message(
                        terminal_key,
                        message,
                    )
                else:
                    outbound = await self._emit(message)
                    delivered = False
                if terminal_key is not None and message is not None and delivered:
                    self._remember_terminal_delivery(terminal_key)
        else:
            message = (
                replay_message
                if replay_prepared
                else self.projector.project_notification(notification, self.store)
            )
            if not replay_prepared:
                self._attach_native_delivery_id(message, notification, namespace="projection")
                if callable(capture_projection):
                    capture_projection(message)
            outbound = await self._emit(message)
        self.store.update_native_appserver_event(
            journal_sequence,
            outcome="projected" if message is not None else "ingested",
        )
        return outbound

    async def handle_server_request(self, request: dict) -> list[OutboundMessage]:
        journal_entry = record_native_appserver_journal(self.store, request)
        gate_outcome = await self._buffer_thread_output(
            kind="server_request",
            payload=request,
            journal_sequence=journal_entry.sequence,
        )
        if gate_outcome is not None:
            self.store.update_native_appserver_event(
                journal_entry.sequence,
                outcome="buffered",
                note="waiting for thread switch response delivery",
            )
            return []
        return await self._process_server_request(request, journal_entry.sequence)

    async def _process_server_request(
        self,
        request: dict,
        journal_sequence: int,
    ) -> list[OutboundMessage]:
        if request.get("method") == "currentTime/read":
            transport_request_id = self._transport_request_id(request)
            if transport_request_id is not None:
                await self.backend.reply_to_transport_request(
                    transport_request_id,
                    {"currentTimeAt": int(self.store.clock())},
                    connection_epoch=self._connection_epoch(request),
                )
                self.store.update_native_appserver_event(journal_sequence, outcome="resolved")
            else:
                self.store.update_native_appserver_event(
                    journal_sequence,
                    outcome="rejected",
                    note="currentTime/read missing transport request id",
                )
            return []
        message = self.projector.project_notification(request, self.store)
        self._attach_native_delivery_id(message, request, namespace="request")
        event = normalize_appserver_message(request)
        if self.native_requests.delegate_to_peer_host(
            request,
            journal_sequence=journal_sequence,
        ):
            self.store.update_native_appserver_event(
                journal_sequence,
                outcome="delegated",
                note="host-owned request deferred for a peer or native fallback",
            )
            return []
        routed = bool(
            event.request_id
            and self.store.get_pending_request(event.request_id) is not None
            and message is not None
        )
        if routed:
            try:
                outbound, still_pending = await asyncio.wait_for(
                    self._emit_required_with_retry(
                        message,
                        pending_request_id=event.request_id,
                    ),
                    timeout=self.server_request_delivery_timeout_s,
                )
            except Exception as exc:
                if event.request_id and self.store.get_pending_request(event.request_id) is None:
                    self.store.update_native_appserver_event(
                        journal_sequence,
                        outcome="resolved",
                        note="native request resolved before IM delivery timeout",
                    )
                    return []
                await self._reject_failed_server_request_delivery(request, event, exc)
                self.store.update_native_appserver_event(
                    journal_sequence,
                    outcome="rejected",
                    note="IM delivery failed for native server request",
                )
                return []
            if not still_pending:
                self.store.update_native_appserver_event(
                    journal_sequence,
                    outcome="resolved",
                    note="native request resolved while IM delivery retry was pending",
                )
                return outbound
            self.store.update_native_appserver_event(journal_sequence, outcome="pending")
            return outbound
        rejection = await self.native_requests.reject_unrouted(request)
        outbound: list[OutboundMessage] = []
        if rejection is not None:
            self._attach_native_delivery_id(rejection, request, namespace="rejection")
            self.store.update_native_appserver_event(
                journal_sequence,
                outcome="rejected",
                note="unsupported or unroutable server request",
            )
            outbound.extend(await self._emit(rejection))
        else:
            self.store.update_native_appserver_event(journal_sequence, outcome="ingested")
        return outbound

    async def _reject_failed_server_request_delivery(self, request: dict, event, exc: Exception) -> None:
        if event.request_id:
            self.store.remove_pending_request(event.request_id)
        transport_request_id = self._transport_request_id(request)
        if transport_request_id is not None:
            try:
                await self.backend.reply_error_to_transport_request(
                    transport_request_id,
                    code=_DELIVERY_FAILED_REQUEST_CODE,
                    message="IMCodex could not deliver this native request to the IM channel",
                    data={
                        "reason": "imDeliveryFailed",
                        "method": event.method,
                        "requestId": event.request_id,
                    },
                    connection_epoch=self._connection_epoch(request),
                )
            except AppServerError:
                pass
        emit_event(
            component="bridge",
            event="bridge.server_request.delivery_failed",
            level="ERROR",
            message=str(exc) or "Native server request IM delivery failed",
            data={
                "method": event.method,
                "request_id": event.request_id,
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
                "error_type": type(exc).__name__,
            },
        )

    def _transport_request_id(self, request: dict) -> str | int | None:
        params = request.get("params")
        if isinstance(params, dict) and params.get("_transport_request_id") is not None:
            return params["_transport_request_id"]
        return request.get("id")

    def _connection_epoch(self, request: dict) -> int | None:
        params = request.get("params")
        if not isinstance(params, dict):
            return None
        try:
            epoch = int(params.get("_connection_epoch") or 0)
        except (TypeError, ValueError):
            return None
        return epoch or None

    async def handle_connection_reset(self, connection_epoch: int) -> None:
        await self._reset_thread_output_admission()
        self.native_requests.cancel_connection_epoch(connection_epoch)
        routes = self.store.invalidate_pending_requests_for_connection(connection_epoch)
        if self.backend.prefers_native_recovery():
            return
        seen_turns: set[tuple[str, str]] = set()
        for route in routes:
            if not route.thread_id or not route.turn_id:
                continue
            key = (route.thread_id, route.turn_id)
            if key in seen_turns:
                continue
            seen_turns.add(key)
            try:
                await self.backend.interrupt_turn(route.thread_id, route.turn_id)
            except AppServerError:
                continue

    async def handle_connection_ready(self, connection_epoch: int) -> dict | None:
        emit_event(
            component="bridge",
            event="bridge.connection.ready",
            message="Bridge app-server connection is ready",
            data={"connection_epoch": connection_epoch},
        )
        if not self.backend.prefers_native_recovery():
            await self._deliver_pending_terminal_once()
            delivery_pending = sum(
                pending.message is not None
                for pending in self.store.list_pending_terminal_deliveries()
            )
            if delivery_pending:
                self._schedule_terminal_delivery_retry()
                return {
                    "status": "degraded",
                    "rehydration": {"deliveryPending": delivery_pending},
                }
            return None
        async with self._rehydration_lock:
            result = await self.backend.rehydrate_bound_threads()
        summary = dict(result.get("summary") or {})
        for discarded in result.get("discardedTurns") or []:
            if not isinstance(discarded, dict):
                continue
            self.projector.discard_recovered_turn(
                thread_id=str(discarded.get("threadId") or ""),
                turn_id=str(discarded.get("turnId") or ""),
            )
        for recovered in result.get("recoveredTurns") or []:
            if not isinstance(recovered, dict) or not isinstance(recovered.get("turn"), dict):
                continue
            thread_id = str(recovered.get("threadId") or "")
            turn = recovered["turn"]
            turn_id = str(turn.get("id") or turn.get("turnId") or "")
            if not thread_id or not turn_id:
                continue
            self._pending_recovered_turns[(thread_id, turn_id)] = turn
        delivery_failed = 0
        for terminal_key, turn in list(self._pending_recovered_turns.items()):
            thread_id, turn_id = terminal_key
            if self.store.find_binding_by_thread_id(thread_id) is None:
                self.projector.discard_recovered_turn(thread_id=thread_id, turn_id=turn_id)
                self._pending_recovered_turns.pop(terminal_key, None)
                continue
            try:
                async with self._terminal_projection_lock:
                    if terminal_key in self._recent_terminal_deliveries:
                        self.projector.discard_recovered_turn(
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                        self._pending_recovered_turns.pop(terminal_key, None)
                        continue
                    message = self.projector.project_recovered_turn(
                        thread_id=thread_id,
                        turn=turn,
                        store=self.store,
                    )
                    if message is None:
                        delivery_failed += 1
                        emit_event(
                            component="bridge",
                            event="bridge.thread_rehydrate.empty_terminal",
                            level="ERROR",
                            message="Recovered terminal turn did not produce a deliverable message",
                            data={"thread_id": thread_id, "turn_id": turn_id},
                        )
                        continue
                    self._attach_delivery_id(
                        message,
                        {
                            "method": "bridge/terminalTurn",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        },
                        namespace="terminal",
                        terminal_key=terminal_key,
                    )
                    _, delivered = await asyncio.wait_for(
                        self._deliver_terminal_message(terminal_key, message),
                        timeout=_RECOVERY_DELIVERY_TIMEOUT_S,
                    )
                    self._pending_recovered_turns.pop(terminal_key, None)
                    if delivered:
                        self._remember_terminal_delivery(terminal_key)
            except Exception as exc:
                delivery_failed += 1
                emit_event(
                    component="bridge",
                    event="bridge.thread_rehydrate.delivery_failed",
                    level="ERROR",
                    message=str(exc) or "Recovered turn delivery failed",
                    data={
                        "thread_id": thread_id,
                        "turn_id": turn.get("id") or turn.get("turnId"),
                        "error_type": type(exc).__name__,
                    },
                )
        if delivery_failed:
            summary["deliveryFailed"] = delivery_failed
        await self._deliver_pending_terminal_once()
        delivery_pending = sum(
            pending.message is not None
            for pending in self.store.list_pending_terminal_deliveries()
        )
        if delivery_pending:
            summary["deliveryPending"] = delivery_pending
            self._schedule_terminal_delivery_retry()
        degraded = (
            summary.get("failed", 0)
            + summary.get("unverified", 0)
            + summary.get("deliveryFailed", 0)
            + summary.get("deliveryPending", 0)
        )
        return {
            "status": "degraded" if degraded else "connected",
            "rehydration": summary,
        }

    async def _emit(self, message: OutboundMessage | None) -> list[OutboundMessage]:
        if message is None:
            return []
        if self.outbound_sink is not None:
            await self.outbound_sink.send_message(message)
        return [message]

    async def _emit_required(self, message: OutboundMessage) -> list[OutboundMessage]:
        if self.outbound_sink is None:
            raise RuntimeError("No outbound IM sink is available")
        await self.outbound_sink.send_message(message)
        return [message]

    async def _emit_required_with_retry(
        self,
        message: OutboundMessage,
        *,
        pending_request_id: str,
    ) -> tuple[list[OutboundMessage], bool]:
        attempt = 0
        while True:
            if self.store.get_pending_request(pending_request_id) is None:
                return [], False
            try:
                outbound = await self._emit_required(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.store.get_pending_request(pending_request_id) is None:
                    return [], False
                delay_s = _SERVER_REQUEST_DELIVERY_RETRY_DELAYS_S[
                    min(attempt, len(_SERVER_REQUEST_DELIVERY_RETRY_DELAYS_S) - 1)
                ]
                attempt += 1
                await asyncio.sleep(delay_s)
                continue
            return outbound, self.store.get_pending_request(pending_request_id) is not None

    def _terminal_delivery_key(self, event, message: OutboundMessage | None) -> tuple[str, str] | None:
        if message is None or message.message_type != "turn_result":
            return None
        return self._event_terminal_key(event)

    @staticmethod
    def _event_terminal_key(event) -> tuple[str, str] | None:
        turn_id = event.turn_id
        if not turn_id and isinstance(event.payload.get("turn"), dict):
            turn_id = str(event.payload["turn"].get("id") or event.payload["turn"].get("turnId") or "")
        if not event.thread_id or not turn_id:
            return None
        return event.thread_id, turn_id

    def _terminal_delivery_is_staged(self, terminal_key: tuple[str, str] | None) -> bool:
        if terminal_key is None:
            return False
        return any(
            pending.turn_id == terminal_key[1] and pending.message is not None
            for pending in self.store.list_pending_terminal_deliveries(terminal_key[0])
        )

    def _attach_delivery_id(
        self,
        message: OutboundMessage | None,
        native_message: dict,
        *,
        namespace: str,
        terminal_key: tuple[str, str] | None,
    ) -> None:
        if terminal_key is None:
            self._attach_native_delivery_id(message, native_message, namespace=namespace)
            return
        self._attach_native_delivery_id(
            message,
            {
                "method": "bridge/terminalTurn",
                "params": {"threadId": terminal_key[0], "turnId": terminal_key[1]},
            },
            namespace="terminal",
        )

    def _remember_terminal_delivery(self, terminal_key: tuple[str, str]) -> None:
        self._recent_terminal_deliveries.pop(terminal_key, None)
        self._recent_terminal_deliveries[terminal_key] = None
        while len(self._recent_terminal_deliveries) > _RECENT_TERMINAL_DELIVERY_LIMIT:
            oldest = next(iter(self._recent_terminal_deliveries))
            self._recent_terminal_deliveries.pop(oldest, None)

    @staticmethod
    def _attach_native_delivery_id(
        message: OutboundMessage | None,
        native_message: dict,
        *,
        namespace: str,
    ) -> None:
        if message is None or message.metadata.get("delivery_id"):
            return
        canonical = json.dumps(native_message, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256()
        for value in (
            namespace,
            message.channel_id,
            message.conversation_id,
            message.message_type,
            canonical,
        ):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        message.metadata["delivery_id"] = f"imcodex:native:{digest.hexdigest()}"

    @staticmethod
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
        if message_type in _SYSTEM_MESSAGE_TYPES and not text.startswith(_SYSTEM_PREFIX):
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
            "native.respond",
            "native.error",
            "threads.exit",
            "approval.accept",
            "approval.deny",
            "approval.cancel",
        }:
            return "status"
        if action.endswith(".invalid") or action.endswith(".missing") or ".missing" in action or action == "unknown":
            return "error"
        if action in {"thread.read.none", "turn.stop.none"}:
            return "command_result"
        return "command_result"

    async def _request_reply_failure(
        self,
        inbound: InboundMessage,
        request_id: str | None,
        action: str,
        error: AppServerError | KeyError,
    ) -> list[OutboundMessage]:
        if self._is_expired_server_request_error(error):
            route = self.store.match_pending_request(
                inbound.channel_id,
                inbound.conversation_id,
                request_id,
            )
            if route is not None and route.thread_id and route.turn_id:
                active = self.store.get_active_turn(route.thread_id)
                if active is not None and active[0] == route.turn_id:
                    try:
                        await self.backend.interrupt_active_turn(inbound.channel_id, inbound.conversation_id)
                    except AppServerError:
                        return [
                            self._message(
                                inbound,
                                "status",
                                f"Request {request_id} is out of sync with Codex and the active turn could not be stopped automatically. Try /stop.",
                                request_id=request_id,
                            )
                        ]
                    self.store.remove_pending_request(request_id or "")
                    return [
                        self._message(
                            inbound,
                            "status",
                            f"Request {request_id} is out of sync with Codex. I stopped the active turn so you can continue.",
                            request_id=request_id,
                        )
                    ]
            self.store.remove_pending_request(request_id or "")
            return [
                self._message(
                    inbound,
                    "status",
                    f"Request {request_id} is no longer pending.",
                    request_id=request_id,
                )
            ]
        verb = "approval" if action.startswith("approval.") else "answer"
        return [
            self._message(
                inbound,
                "status",
                f"Request {request_id} {verb} could not be sent to Codex right now. Try again.",
                request_id=request_id,
            )
        ]

    async def _resolve_projected_requests(
        self,
        *,
        inbound: InboundMessage,
        request_ids: list[str],
        decision: str,
        action: str,
        continue_on_failure: bool,
    ) -> list[OutboundMessage] | None:
        for request_id in request_ids:
            payload = self.native_requests.resolution_payload(request_id, decision)
            try:
                await self.backend.reply_to_server_request(request_id, payload)
            except (AppServerError, KeyError) as exc:
                failure = await self._request_reply_failure(inbound, request_id, action, exc)
                if not continue_on_failure:
                    return failure
        return None

    def _is_expired_server_request_error(self, error: AppServerError | KeyError) -> bool:
        if isinstance(error, KeyError):
            return True
        return "unknown pending request" in str(error).lower()

    def _render_onboarding(self) -> str:
        return "\n".join(
            [
                "Before we start, I need a working folder.",
                "",
                "Use /cwd playground for a default workspace.",
                "Use /cwd <path> to point me at an existing folder.",
            ]
        )

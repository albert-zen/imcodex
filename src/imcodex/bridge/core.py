from __future__ import annotations

import asyncio

from ..appserver import AppServerError, StaleThreadBindingError, ThreadSelectionError
from ..models import InboundMessage, OutboundMessage
from ..observability.message_trace import ensure_trace_id, text_preview, text_sha256
from ..observability.runtime import emit_event
from .native_events import (
    clamp_native_events_limit,
    record_native_appserver_journal,
    render_native_events,
    select_native_events,
)
from .rendering import BridgeRenderingMixin
from .server_requests import NativeRequestPolicy
from .settings import (
    render_credits,
    render_fast_mode,
    render_models,
    render_permission_modes,
    render_permission_set_result,
    render_reasoning_effort,
)
from .thread_history import render_thread_history
from .thread_views import ThreadViewMixin


_SYSTEM_MESSAGE_TYPES = frozenset({"accepted", "status", "error"})
_SYSTEM_PREFIX = "[System] "


class BridgeService(ThreadViewMixin, BridgeRenderingMixin):
    def __init__(
        self,
        *,
        store,
        backend,
        command_router,
        projector,
        outbound_sink=None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.command_router = command_router
        self.projector = projector
        self.outbound_sink = outbound_sink
        self._rehydration_lock = asyncio.Lock()
        self.native_requests = NativeRequestPolicy(store=store, backend=backend)

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        trace_id = ensure_trace_id(message)
        message_kind = "command" if message.text.startswith("/") else "text"
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
            },
        )
        try:
            if message_kind == "command":
                outbound = await self._handle_command(message)
            else:
                outbound = await self._handle_text(message)
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

    async def _handle_text(self, message: InboundMessage) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.bootstrap_cwd is None and binding.thread_id is None:
            return [self._message(message, "status", self._render_onboarding())]
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
            submission = await self.backend.submit_text(message.channel_id, message.conversation_id, message.text)
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
            result = await self.backend.read_config(message.channel_id, message.conversation_id)
            return [self._message(message, "command_result", render_reasoning_effort(result))]
        if response.action == "settings.fast.read":
            result = await self.backend.read_config(message.channel_id, message.conversation_id)
            return [self._message(message, "command_result", render_fast_mode(result))]
        if response.action == "credits.read":
            try:
                result = await self.backend.read_account_credits()
            except AppServerError as exc:
                text = f"Credits could not be queried from Codex right now: {self._safe_appserver_error(exc)}. Try again in a moment."
                return [self._message(message, "status", text)]
            return [self._message(message, "command_result", render_credits(result))]
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
            await self.backend.set_default_model(None if response.payload is None else response.payload.get("model"))
            return [self._message(message, "status", response.text)]
        if response.action == "settings.reasoning.write":
            payload = response.payload or {}
            await self.backend.write_config_value(
                key_path="model_reasoning_effort",
                value=payload.get("effort"),
            )
            return [self._message(message, "status", response.text)]
        if response.action == "settings.fast.write":
            payload = response.payload or {}
            edits = [
                {
                    "keyPath": edit["key_path"],
                    "value": edit["value"],
                    "mergeStrategy": edit["merge_strategy"],
                }
                for edit in list(payload.get("edits") or [])
            ]
            await self.backend.batch_write_config(edits=edits, reload_user_config=False)
            return [self._message(message, "status", response.text)]
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
            try:
                payload = await self.backend.read_thread_history(
                    message.channel_id,
                    message.conversation_id,
                    limit=6,
                )
            except AppServerError as exc:
                text = f"Thread history could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
                return [self._message(message, "command_result", text)]
            return [self._message(message, "command_result", render_thread_history(payload, limit=6))]
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            return [self._message(message, "status", f"Started thread {thread_id}.")]
        if response.action == "thread.fork":
            try:
                snapshot = await self.backend.fork_thread(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                return [self._message(message, "status", f"Thread could not be forked: {self._safe_appserver_error(exc)}.")]
            if snapshot.cwd:
                return [self._message(message, "status", f"Forked to {self._thread_label(snapshot)}.\nCWD: {snapshot.cwd}")]
            return [self._message(message, "status", f"Forked to {self._thread_label(snapshot)}.")]
        if response.action == "thread.rename":
            name = str((response.payload or {}).get("name") or "").strip()
            try:
                await self.backend.rename_thread(message.channel_id, message.conversation_id, name)
            except AppServerError as exc:
                return [self._message(message, "status", f"Thread could not be renamed: {self._safe_appserver_error(exc)}.")]
            return [self._message(message, "status", f"Renamed thread to {name}.")]
        if response.action == "thread.compact":
            try:
                await self.backend.compact_thread(message.channel_id, message.conversation_id)
            except AppServerError as exc:
                return [self._message(message, "status", f"Compaction could not be started: {self._safe_appserver_error(exc)}.")]
            return [self._message(message, "status", "Compaction started.")]
        if response.action == "thread.pick":
            context = self.store.get_thread_browser_context(message.channel_id, message.conversation_id)
            if context is None:
                return [self._message(message, "error", "Use /threads first.")]
            index = int((response.payload or {}).get("index") or 0)
            if index < 0 or index >= len(context.thread_ids):
                return [self._message(message, "error", "Pick a number from the current page.")]
            thread_id = context.thread_ids[index]
            try:
                attached_id = await self.backend.attach_thread(message.channel_id, message.conversation_id, thread_id)
            except ThreadSelectionError as exc:
                return [self._message(message, "error", str(exc))]
            except AppServerError as exc:
                return [self._message(message, "status", f"Thread could not be attached: {self._safe_appserver_error(exc)}.")]
            snapshot = self.store.get_thread_snapshot(attached_id)
            label = self._thread_label(snapshot) if snapshot is not None else attached_id
            if snapshot is not None and snapshot.cwd:
                return [self._message(message, "status", f"Switched to {label}.\nCWD: {snapshot.cwd}")]
            return [self._message(message, "status", f"Switched to {label}.")]
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
                await self.backend.attach_thread(message.channel_id, message.conversation_id, snapshot.thread_id)
            except ThreadSelectionError as exc:
                return [self._message(message, "error", str(exc))]
            except Exception as exc:
                return [self._message(message, "status", f"Thread could not be attached: {self._safe_exception_text(exc)}.")]
            if snapshot.cwd:
                return [self._message(message, "status", f"Attached to {self._thread_label(snapshot)}.\nCWD: {snapshot.cwd}")]
            return [self._message(message, "status", f"Attached to {self._thread_label(snapshot)}.")]
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
        journal_entry = record_native_appserver_journal(self.store, notification)
        message = self.projector.project_notification(notification, self.store)
        self.store.update_native_appserver_event(
            journal_entry.sequence,
            outcome="projected" if message is not None else "ingested",
        )
        return await self._emit(message)

    async def handle_server_request(self, request: dict) -> list[OutboundMessage]:
        journal_entry = record_native_appserver_journal(self.store, request)
        if request.get("method") == "currentTime/read":
            transport_request_id = self._transport_request_id(request)
            if transport_request_id is not None:
                await self.backend.reply_to_transport_request(
                    transport_request_id,
                    {"currentTimeAt": int(self.store.clock())},
                )
                self.store.update_native_appserver_event(journal_entry.sequence, outcome="resolved")
            else:
                self.store.update_native_appserver_event(
                    journal_entry.sequence,
                    outcome="rejected",
                    note="currentTime/read missing transport request id",
                )
            return []
        message = self.projector.project_notification(request, self.store)
        outbound = await self._emit(message)
        rejection = await self.native_requests.reject_unrouted(request)
        if rejection is not None:
            self.store.update_native_appserver_event(
                journal_entry.sequence,
                outcome="rejected",
                note="unsupported or unroutable server request",
            )
            outbound.extend(await self._emit(rejection))
        else:
            outcome = "pending" if message is not None and journal_entry.direction == "server_request" else "ingested"
            self.store.update_native_appserver_event(journal_entry.sequence, outcome=outcome)
        return outbound

    def _transport_request_id(self, request: dict) -> str | int | None:
        params = request.get("params")
        if isinstance(params, dict) and params.get("_transport_request_id") is not None:
            return params["_transport_request_id"]
        return request.get("id")

    async def handle_connection_reset(self, connection_epoch: int) -> None:
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

    async def handle_connection_ready(self, connection_epoch: int) -> None:
        emit_event(
            component="bridge",
            event="bridge.connection.ready",
            message="Bridge app-server connection is ready",
            data={"connection_epoch": connection_epoch},
        )
        if not self.backend.prefers_native_recovery():
            return
        async with self._rehydration_lock:
            await self.backend.rehydrate_bound_threads()

    async def _emit(self, message: OutboundMessage | None) -> list[OutboundMessage]:
        if message is None:
            return []
        if self.outbound_sink is not None:
            await self.outbound_sink.send_message(message)
        return [message]

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

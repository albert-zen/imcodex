from __future__ import annotations

import asyncio
import json

from ..appserver import AppServerError, StaleThreadBindingError, ThreadSelectionError
from ..models import InboundMessage, OutboundMessage


_SYSTEM_MESSAGE_TYPES = frozenset({"accepted", "status", "error"})
_SYSTEM_PREFIX = "[System] "
_THREADS_PAGE_SIZE = 5


class BridgeService:
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

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        if message.text.startswith("/"):
            return await self._handle_command(message)
        return await self._handle_text(message)

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

    async def _handle_command(self, message: InboundMessage) -> list[OutboundMessage]:
        response = self.command_router.handle(message.channel_id, message.conversation_id, message.text)
        if response.action == "threads.query":
            try:
                payload = response.payload or {}
                text = await self._render_threads(
                    message,
                    response.include_all,
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
            return [self._message(message, "command_result", self._render_models(result))]
        if response.action == "settings.permission.read":
            result = await self.backend.read_config(message.channel_id, message.conversation_id)
            return [self._message(message, "command_result", self._render_permission_modes(result))]
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
        if response.action == "settings.permission.write":
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
        if response.action == "thread.read.query":
            return [self._message(message, "command_result", await self._render_thread(message, response.thread_id))]
        if response.action == "thread.new":
            thread_id = await self.backend.create_new_thread(message.channel_id, message.conversation_id)
            return [self._message(message, "status", f"Started thread {thread_id}.")]
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
                return self._request_reply_failure(message, response.request_id, response.action, exc)
        message_type = self._command_message_type(response.action)
        return [self._message(message, message_type, response.text, request_id=response.request_id)]

    async def handle_notification(self, notification: dict) -> list[OutboundMessage]:
        message = self.projector.project_notification(notification, self.store)
        return await self._emit(message)

    async def handle_server_request(self, request: dict) -> list[OutboundMessage]:
        message = self.projector.project_notification(request, self.store)
        return await self._emit(message)

    async def handle_connection_reset(self, connection_epoch: int) -> None:
        routes = self.store.invalidate_pending_requests_for_connection(connection_epoch)
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

    async def _emit(self, message: OutboundMessage | None) -> list[OutboundMessage]:
        if message is None:
            return []
        if self.outbound_sink is not None:
            await self.outbound_sink.send_message(message)
        return [message]

    async def _render_threads(
        self,
        message: InboundMessage,
        include_all: bool,
        *,
        page: int = 1,
        query: str | None = None,
    ) -> str:
        threads = await self.backend.list_threads(message.channel_id, message.conversation_id, include_all=include_all)
        if query:
            threads = [snapshot for snapshot in threads if self._matches_thread_query(snapshot, query)]
        if not threads:
            self.store.set_thread_browser_context(
                message.channel_id,
                message.conversation_id,
                thread_ids=[],
                page=1,
                total=1,
                query=query,
                include_all=include_all,
            )
            return "\n".join(
                [
                    "Threads (Page 1/1)",
                    "(none)",
                    "Use /threads <keyword> to filter, or /new to start a fresh thread.",
                ]
            )
        page_count = max(1, (len(threads) + _THREADS_PAGE_SIZE - 1) // _THREADS_PAGE_SIZE)
        safe_page = min(max(page, 1), page_count)
        start = (safe_page - 1) * _THREADS_PAGE_SIZE
        visible = threads[start : start + _THREADS_PAGE_SIZE]
        self.store.set_thread_browser_context(
            message.channel_id,
            message.conversation_id,
            thread_ids=[snapshot.thread_id for snapshot in visible],
            page=safe_page,
            total=page_count,
            query=query,
            include_all=include_all,
        )
        lines = [f"Threads (Page {safe_page}/{page_count})"]
        for index, snapshot in enumerate(visible, start=1):
            details = [snapshot.status]
            if snapshot.thread_id == self.store.get_binding(message.channel_id, message.conversation_id).thread_id:
                details.append("current")
            lines.append(f"{index}. {self._thread_label(snapshot)} ({', '.join(details)})")
        actions = ["Use /pick <n> to switch", "/new to start fresh", "/exit to close"]
        if safe_page < page_count:
            actions.insert(1, "/next for more")
        if safe_page > 1:
            actions.insert(1, "/prev for previous")
        if query is None:
            actions.append("/threads <keyword> to filter")
        return "\n".join(lines + ["; ".join(actions) + "."])

    async def _render_status(self, message: InboundMessage) -> str:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        cwd = self.store.current_cwd(message.channel_id, message.conversation_id) or "(none)"
        config = await self._read_status_config(message.channel_id, message.conversation_id)
        current_config = config.get("config") if isinstance(config.get("config"), dict) else {}
        if binding.thread_id is None:
            thread_label = "(none)"
            state = "Idle"
        else:
            snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, binding.thread_id)
            if snapshot is None:
                thread_label = binding.thread_id
                state = "Unavailable"
            else:
                thread_label = self._thread_label(snapshot)
                cwd = snapshot.cwd or cwd
                active = self.store.get_active_turn(binding.thread_id)
                state = "Working" if active and active[1] == "inProgress" else self._human_state(snapshot.status)
        return "\n".join(
            [
                "Status",
                "",
                f"CWD: {cwd}",
                f"Thread: {thread_label}",
                f"State: {state}",
                f"Model: {self._current_model_label(current_config)}",
                f"Permissions: {self._permission_mode_label(current_config)}",
                f"Bridge visibility: {self._bridge_visibility_label(binding)}",
                f"Pending approvals: {len(self.store.list_pending_requests(message.channel_id, message.conversation_id, kind='approval'))}",
            ]
        )

    async def _render_thread(self, message: InboundMessage, thread_id: str | None) -> str:
        if thread_id is None:
            return "No active thread."
        try:
            snapshot = await self.backend.read_thread(message.channel_id, message.conversation_id, thread_id)
        except AppServerError as exc:
            return f"Current thread {thread_id} could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
        if snapshot is None:
            return f"Current thread {thread_id} is no longer available in Codex."
        return "\n".join(
            [
                f"Thread: {self._thread_label(snapshot)}",
                f"Thread id: {snapshot.thread_id}",
                f"CWD: {snapshot.cwd or '(unknown)'}",
                f"Path: {snapshot.path or snapshot.cwd or '(unknown)'}",
                f"Status: {snapshot.status}",
                f"Source: {snapshot.source or 'unknown'}",
            ]
        )

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
        )

    def _command_message_type(self, action: str) -> str:
        if action in {
            "project.cwd",
            "settings.view",
            "settings.visibility",
            "settings.model",
            "settings.permission.write",
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

    def _render_models(self, payload: dict) -> str:
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            return "Models\n\nCurrent: Unknown"
        current = next((item for item in items if isinstance(item, dict) and item.get("isDefault")), None)
        current_label = self._model_label(current) if isinstance(current, dict) else "Unknown"
        lines = ["Models", "", f"Current: {current_label}", "", "Available:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {self._model_label(item)}")
        lines.append("")
        lines.append("Use /model <model-id> to switch directly.")
        return "\n".join(lines)

    def _render_permission_modes(self, payload: dict) -> str:
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        current = self._permission_mode_label(config)
        return "\n".join(
            [
                "Permission Modes",
                "",
                f"Current: {current}",
                "",
                "- /permission default",
                "- /permission read-only",
                "- /permission full-access",
            ]
        )

    def _render_config(self, payload: dict, key_path: object | None) -> str:
        config = payload.get("config")
        if key_path is not None and isinstance(config, dict):
            return self._render_json(self._lookup_config_value(config, str(key_path)))
        return self._render_json(config if config is not None else payload)

    def _lookup_config_value(self, payload: dict, key_path: str) -> object:
        current: object = payload
        for part in key_path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _render_json(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)

    def _thread_label(self, snapshot) -> str:
        for candidate in (snapshot.name, snapshot.preview, snapshot.path, snapshot.cwd):
            if not candidate:
                continue
            text = str(candidate).strip()
            if text:
                if "\\" in text or "/" in text:
                    text = text.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                return text
        return snapshot.thread_id

    def _matches_thread_query(self, snapshot, query: str) -> bool:
        normalized_query = self._normalize_text(query)
        if not normalized_query:
            return True
        candidates = [
            snapshot.thread_id,
            snapshot.name or "",
            snapshot.preview or "",
            snapshot.cwd or "",
            snapshot.path or "",
            self._thread_label(snapshot),
        ]
        return any(normalized_query in self._normalize_text(candidate) for candidate in candidates if candidate)

    def _normalize_text(self, value: str) -> str:
        return " ".join(value.lower().replace("_", " ").replace("-", " ").split())

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
            try:
                await self.backend.reply_to_server_request(request_id, {"decision": decision})
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

    def _model_label(self, item: dict) -> str:
        display = str(item.get("displayName") or item.get("model") or item.get("id") or "unknown")
        model_id = str(item.get("model") or item.get("id") or display)
        if display == model_id:
            return display
        return f"{display} ({model_id})"

    def _current_model_label(self, config: dict) -> str:
        model = config.get("model")
        if not model:
            return "Default"
        return str(model)

    def _permission_mode_label(self, config: dict) -> str:
        approval = str(config.get("approval_policy") or "")
        sandbox = str(config.get("sandbox_mode") or "")
        if approval == "on-request" and sandbox == "workspace-write":
            return "Default"
        if approval == "on-request" and sandbox == "read-only":
            return "Read Only"
        if approval == "never" and sandbox == "danger-full-access":
            return "Full Access"
        details = ", ".join(part for part in (approval, sandbox) if part)
        return f"Custom ({details})" if details else "Custom"

    def _bridge_visibility_label(self, binding) -> str:
        return binding.visibility_profile.replace("-", " ").title()

    def _human_state(self, status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"inprogress", "in_progress", "working", "running"}:
            return "Working"
        if normalized == "completed":
            return "Completed"
        if normalized == "failed":
            return "Failed"
        return "Idle" if normalized == "idle" else str(status or "Idle").title()

    async def _read_status_config(self, channel_id: str, conversation_id: str) -> dict:
        try:
            return await asyncio.wait_for(
                self.backend.read_config(channel_id, conversation_id),
                timeout=2.5,
            )
        except (asyncio.TimeoutError, AppServerError):
            return {"config": {}}

    def _safe_appserver_error(self, error: AppServerError) -> str:
        return self._safe_exception_text(error)

    def _safe_exception_text(self, error: Exception) -> str:
        text = " ".join(str(error).split())
        lowered = text.lower()
        if not text:
            return "unexpected upstream error"
        if any(marker in lowered for marker in ("<html", "<!doctype", "</html", "separator is not found", "chunk exceed the limit")):
            return "unexpected upstream error"
        if len(text) > 180:
            return "unexpected upstream error"
        return text

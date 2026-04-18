from __future__ import annotations

from ..appserver import normalize_appserver_message
from ..models import OutboundMessage
from .message_pump import MessagePump


class MessageProjector:
    def __init__(self, *, message_pump: MessagePump | None = None) -> None:
        self.message_pump = message_pump or MessagePump()

    def project_notification(self, notification: dict, store) -> OutboundMessage | None:
        event = normalize_appserver_message(notification)
        if event.kind in {"approval_request", "question_request"}:
            binding = store.find_binding_by_thread_id(event.thread_id)
            if binding is None:
                return None
            if self._is_stale_turn(event.thread_id, event.turn_id, store):
                return None
            route = store.upsert_pending_request(
                request_id=event.request_id or "",
                channel_id=binding.channel_id,
                conversation_id=binding.conversation_id,
                thread_id=event.thread_id or None,
                turn_id=event.turn_id or None,
                kind="question" if event.kind == "question_request" else "approval",
                request_method=event.method,
                transport_request_id=event.payload.get("_transport_request_id"),
                connection_epoch=int(event.payload.get("_connection_epoch") or 0),
                payload=event.payload,
            )
            return self._attach_route(
                binding.channel_id,
                binding.conversation_id,
                self._render_pending_request(route),
            )
        if event.kind == "request_resolved":
            if event.request_id:
                store.remove_pending_request(event.request_id)
            return None
        if event.kind == "turn_started":
            turn = event.payload.get("turn") or {}
            turn_id = str(turn.get("id") or event.turn_id or "")
            status = str(turn.get("status") or "inProgress")
            if event.thread_id and turn_id:
                active = store.get_active_turn(event.thread_id)
                if active is None or active[0] == turn_id:
                    if not store.is_turn_suppressed(event.thread_id, turn_id):
                        store.note_active_turn(event.thread_id, turn_id, status)
            return None
        if event.kind == "thread_name_updated":
            name = str(event.payload.get("name") or "").strip()
            if name:
                store.update_thread_snapshot(event.thread_id, name=name)
            return None
        if event.kind in {
            "thread_status_changed",
            "thread_compacted",
            "model_rerouted",
            "config_warning",
            "deprecation_notice",
        }:
            if not self._show_system(event.thread_id, store):
                return None
            return self._attach_to_thread(
                event.thread_id,
                store,
                OutboundMessage(
                    channel_id="",
                    conversation_id="",
                    message_type="status",
                    text=self._render_system_event(event),
                ),
            )
        if event.kind == "plan_updated":
            if not self._show_commentary(event.thread_id, store):
                return None
            return self._attach_to_thread(
                event.thread_id,
                store,
                OutboundMessage(
                    channel_id="",
                    conversation_id="",
                    message_type="turn_progress",
                    text=self._render_plan_update(event.payload),
                ),
            )
        if event.kind == "diff_updated":
            if not self._show_commentary(event.thread_id, store):
                return None
            return self._attach_to_thread(
                event.thread_id,
                store,
                OutboundMessage(
                    channel_id="",
                    conversation_id="",
                    message_type="turn_progress",
                    text=self._render_diff_update(event.payload),
                ),
            )
        if event.kind == "agent_delta":
            if self._is_stale_turn(event.thread_id, event.turn_id, store):
                return None
            self.message_pump.record_delta(
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                delta=str(event.payload.get("delta") or ""),
                emit_progress=False,
            )
            return None
        if event.kind == "item_completed":
            return self._attach_to_thread(event.thread_id, store, self._capture_item_completed(event.payload, store))
        if event.kind == "turn_completed":
            turn = event.payload.get("turn") or {}
            turn_id = str(turn.get("id") or event.turn_id or "")
            status = str(turn.get("status") or "")
            if self._is_stale_turn(event.thread_id, turn_id, store):
                if event.thread_id and turn_id:
                    store.complete_turn(event.thread_id, turn_id, status)
                self.message_pump.discard_turn(thread_id=event.thread_id, turn_id=turn_id)
                return None
            if event.thread_id and turn_id:
                store.complete_turn(event.thread_id, turn_id, status)
            return self._attach_to_thread(
                event.thread_id,
                store,
                self.message_pump.finalize_turn(thread_id=event.thread_id, turn_id=turn_id, status=status),
            )
        return None

    def _capture_item_completed(self, params: dict, store) -> OutboundMessage | None:
        item = params.get("item") or {}
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        if self._is_stale_turn(thread_id, turn_id, store):
            return None
        item_type = item.get("type")
        if item_type == "agentMessage":
            return self.message_pump.record_agent_message(
                thread_id=thread_id,
                turn_id=turn_id,
                phase=item.get("phase"),
                text=str(item.get("text") or ""),
                emit_commentary=self._show_commentary(thread_id, store),
            )
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            if not command:
                return None
            return self.message_pump.record_command(
                thread_id=thread_id,
                turn_id=turn_id,
                command=command,
                emit_progress=self._show_toolcalls(thread_id, store),
            )
        if item_type == "fileChange":
            paths = [str(change.get("path")) for change in item.get("changes", []) if change.get("path")]
            return self.message_pump.record_file_change(
                thread_id=thread_id,
                turn_id=turn_id,
                paths=paths,
                emit_progress=self._show_toolcalls(thread_id, store),
            )
        return None

    def _attach_to_thread(self, thread_id: str, store, message: OutboundMessage | None) -> OutboundMessage | None:
        if message is None:
            return None
        binding = store.find_binding_by_thread_id(thread_id)
        if binding is None:
            return None
        return self._attach_route(binding.channel_id, binding.conversation_id, message)

    def _attach_route(self, channel_id: str, conversation_id: str, message: OutboundMessage) -> OutboundMessage:
        message.channel_id = channel_id
        message.conversation_id = conversation_id
        return message

    def _render_pending_request(self, route) -> OutboundMessage:
        handle = route.request_id[:8]
        if route.kind == "question":
            questions = route.payload.get("questions") or []
            lines = [
                f"[request {handle}] Codex needs more input.",
                f"Native request id: {route.request_id}",
            ]
            for question in questions:
                question_id = str(question.get("id") or "question")
                question_text = str(question.get("question") or "")
                lines.append(f"- {question_id}: {question_text}")
            first_question_id = "key"
            if questions:
                first_question_id = str(questions[0].get("id") or "key")
            lines.append(f"Reply with /answer {route.request_id} {first_question_id}=value")
            text = "\n".join(lines)
            return OutboundMessage(channel_id="", conversation_id="", message_type="question_request", text=text, request_id=route.request_id)
        lines = [
            f"[request {handle}] Approval needed.",
            f"Native request id: {route.request_id}",
        ]
        reason = str(route.payload.get("reason") or "").strip()
        command = str(route.payload.get("command") or "").strip()
        cwd = str(route.payload.get("cwd") or "").strip()
        path = str(route.payload.get("path") or "").strip()
        if reason:
            lines.append(reason)
        if command:
            lines.append(command)
        if cwd:
            lines.append(f"CWD: {cwd}")
        if path:
            lines.append(f"Path: {path}")
        lines.append("Use /approve to allow, /deny to reject, or send a new message to cancel and continue.")
        lines.append(f"Target one request with /approve {handle}, /deny {handle}, or /cancel {handle}.")
        text = "\n".join(lines)
        return OutboundMessage(channel_id="", conversation_id="", message_type="approval_request", text=text, request_id=route.request_id)

    def _show_commentary(self, thread_id: str, store) -> bool:
        binding = store.find_binding_by_thread_id(thread_id)
        return True if binding is None else binding.show_commentary

    def _show_toolcalls(self, thread_id: str, store) -> bool:
        binding = store.find_binding_by_thread_id(thread_id)
        return False if binding is None else binding.show_toolcalls

    def _show_system(self, thread_id: str, store) -> bool:
        binding = store.find_binding_by_thread_id(thread_id)
        return False if binding is None else binding.show_system

    def _is_stale_turn(self, thread_id: str, turn_id: str, store) -> bool:
        if not thread_id or not turn_id:
            return False
        if store.is_turn_suppressed(thread_id, turn_id):
            return True
        active = store.get_active_turn(thread_id)
        if active is None:
            return False
        return active[0] != turn_id

    def _render_plan_update(self, params: dict) -> str:
        lines: list[str] = []
        explanation = params.get("explanation")
        if explanation:
            lines.append(str(explanation))
        for entry in params.get("plan", []):
            step = entry.get("step")
            status = entry.get("status")
            if step and status:
                lines.append(f"[{status}] {step}")
        return "\n".join(lines)

    def _render_diff_update(self, params: dict) -> str:
        summary = str(params.get("summary") or "Diff updated.")
        files = [str(path) for path in params.get("files", []) if str(path).strip()]
        lines = [summary]
        if files:
            lines.append("Files:")
            lines.extend(f"- {path}" for path in files)
        return "\n".join(lines)

    def _render_system_event(self, event) -> str:
        if event.kind == "thread_status_changed":
            status = event.payload.get("status")
            if isinstance(status, dict):
                status = status.get("type") or status.get("status")
            return f"Thread status changed: {status or 'updated'}."
        if event.kind == "thread_compacted":
            summary = str(event.payload.get("summary") or "").strip()
            return "Thread compacted." if not summary else f"Thread compacted. {summary}"
        if event.kind == "model_rerouted":
            message = str(event.payload.get("message") or "").strip()
            return "Model rerouted." if not message else message
        if event.kind == "config_warning":
            message = str(event.payload.get("message") or "").strip()
            return "Config warning." if not message else message
        if event.kind == "deprecation_notice":
            message = str(event.payload.get("message") or "").strip()
            return "Deprecation notice." if not message else message
        return event.method

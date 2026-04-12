from __future__ import annotations

from ..models import OutboundMessage, PendingRequest
from .message_pump import MessagePump
from .visibility import VisibilityClassifier


class MessageProjector:
    def __init__(
        self,
        *,
        request_registry=None,
        turn_state=None,
        message_pump: MessagePump | None = None,
        visibility: VisibilityClassifier | None = None,
    ) -> None:
        self.request_registry = request_registry
        self.turn_state = turn_state
        self.visibility = visibility or VisibilityClassifier()
        self.message_pump = message_pump or MessagePump(
            progress_renderer=lambda text: self.render_turn_progress(text=text),
            result_renderer=lambda final_text, command_summaries, changed_files, failed, interrupted: self.render_turn_completed(
                final_text=final_text,
                command_summaries=command_summaries,
                changed_files=changed_files,
                failed=failed,
                interrupted=interrupted,
            ),
        )

    def render_pending_request(self, pending: PendingRequest) -> OutboundMessage:
        if pending.kind == "question":
            questions = pending.payload.get("questions", [])
            body = "\n".join(
                f"- {question.get('id')}: {question.get('question')}" for question in questions
            )
            text = (
                f"[ticket {pending.ticket_id}] Codex needs more input.\n"
                f"{body}\n"
                f"Reply with /answer {pending.ticket_id} "
                f"{questions[0].get('id', 'question')}=value"
            )
            return OutboundMessage(
                channel_id=pending.channel_id,
                conversation_id=pending.conversation_id,
                message_type="question_request",
                text=text,
                ticket_id=pending.ticket_id,
            )
        payload = pending.payload
        summary = payload.get("command") or pending.summary
        lines = [
            f"[ticket {pending.ticket_id}] Approval needed.",
            str(summary),
        ]
        cwd = payload.get("cwd")
        if cwd:
            lines.append(f"CWD: {cwd}")
        if payload.get("path"):
            lines.append(f"Path: {payload['path']}")
        network = payload.get("network") or {}
        host = network.get("host") if isinstance(network, dict) else None
        if host:
            lines.append(f"Network: {host}")
        lines.append(
            f"Use /approve {pending.ticket_id}, /approve-session {pending.ticket_id}, "
            f"/deny {pending.ticket_id}, or /cancel {pending.ticket_id}"
        )
        return OutboundMessage(
            channel_id=pending.channel_id,
            conversation_id=pending.conversation_id,
            message_type="approval_request",
            text="\n".join(lines),
            ticket_id=pending.ticket_id,
        )

    def render_turn_completed(
        self,
        *,
        final_text: str,
        command_summaries: list[str],
        changed_files: list[str],
        failed: bool,
        interrupted: bool,
    ) -> OutboundMessage:
        if not failed and not interrupted:
            lines = [final_text] if final_text else []
        else:
            status = "Turn interrupted." if interrupted else "Turn failed."
            lines = [status, final_text]
        if (failed or interrupted or not final_text) and command_summaries:
            lines.extend(command_summaries)
        if (failed or interrupted or not final_text) and changed_files:
            lines.append("Changed files:")
            lines.extend(f"- {path}" for path in changed_files)
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_result",
            text="\n".join(part for part in lines if part),
        )

    def render_turn_progress(self, *, text: str) -> OutboundMessage:
        return OutboundMessage(
            channel_id="",
            conversation_id="",
            message_type="turn_progress",
            text=text,
        )

    def project_notification(self, notification: dict, store) -> OutboundMessage | None:
        method = notification.get("method")
        params = notification.get("params", {})
        if method == "item/commandExecution/requestApproval":
            return self._project_pending_request(
                store,
                params,
                request_method=method,
                kind="approval",
                summary=params.get("reason") or params.get("command") or "Approve command execution",
            )
        if method == "item/fileChange/requestApproval":
            return self._project_pending_request(
                store,
                params,
                request_method=method,
                kind="approval",
                summary=params.get("reason") or "Approve file changes",
            )
        if method == "item/tool/requestUserInput":
            return self._project_pending_request(
                store,
                params,
                request_method=method,
                kind="question",
                summary="Additional input required",
            )
        if method == "item/agentMessage/delta":
            message = self.message_pump.record_delta(
                thread_id=params.get("threadId", ""),
                turn_id=params.get("turnId", ""),
                delta=params.get("delta", ""),
                emit_progress=self._show_commentary(params.get("threadId", ""), store),
            )
            if message is not None:
                return self._attach_conversation(params.get("threadId", ""), message, store)
            return None
        if method == "turn/started":
            thread_id = params.get("threadId", "")
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            status = turn.get("status")
            if thread_id and turn_id and status:
                if self._is_superseded_turn(thread_id, turn_id, store):
                    return None
                store.note_turn_started(thread_id, turn_id=turn_id, status=status)
                if self.turn_state is not None:
                    self.turn_state.start(thread_id, turn_id)
                    self.turn_state.mark_in_progress(thread_id, turn_id)
            return None
        if method == "serverRequest/resolved":
            request_id = str(params.get("requestId", ""))
            request = store.get_pending_request_by_request_id(request_id)
            if self.request_registry is not None:
                resolved = self.request_registry.resolve_native_request(
                    native_request_id=request_id,
                    resolution=(request.submitted_resolution if request is not None else None)
                    or {"requestId": params.get("requestId")},
                )
                if resolved is not None and self.turn_state is not None and resolved.thread_id and resolved.turn_id:
                    self.turn_state.resolve_request(resolved.thread_id, resolved.turn_id, request_id)
            elif request is not None:
                store.resolve_pending_request(
                    request.ticket_id,
                    request.submitted_resolution or {"requestId": params.get("requestId")},
                    channel_id=request.channel_id,
                    conversation_id=request.conversation_id,
                )
            return None
        if method == "turn/plan/updated":
            if not self._show_commentary(params.get("threadId", ""), store):
                return None
            return self._attach_conversation(
                params.get("threadId", ""),
                self.render_turn_progress(text=self._render_plan_update(params)),
                store,
            )
        if method == "item/completed":
            return self._capture_item_completed(params, store)
        if method == "turn/completed":
            return self._finalize_turn(params, store)
        return None

    def _project_pending_request(
        self,
        store,
        params: dict,
        *,
        request_method: str,
        kind: str,
        summary: str,
    ) -> OutboundMessage | None:
        binding = self._find_binding(store, params.get("threadId"))
        if binding is None:
            return None
        thread_id = params.get("threadId", "")
        turn_id = params.get("turnId", "")
        if thread_id and turn_id and self._is_stale_turn(thread_id, turn_id, store):
            return None
        if self.request_registry is not None:
            request = self.request_registry.open_request(
                channel_id=binding.channel_id,
                conversation_id=binding.conversation_id,
                native_request_id=str(params.get("_request_id", "")) or None,
                request_method=request_method,
                request_kind=kind,
                summary=summary,
                payload=params,
                thread_id=params.get("threadId"),
                turn_id=params.get("turnId"),
                item_id=params.get("itemId"),
            )
        else:
            ticket_id = store.next_ticket_id(binding.channel_id, binding.conversation_id)
            request = store.create_pending_request(
                channel_id=binding.channel_id,
                conversation_id=binding.conversation_id,
                ticket_id=ticket_id,
                kind=kind,
                summary=summary,
                payload=params,
                request_id=str(params.get("_request_id", "")) or None,
                request_method=request_method,
                thread_id=params.get("threadId"),
                turn_id=params.get("turnId"),
                item_id=params.get("itemId"),
            )
        if self.turn_state is not None and request.thread_id and request.turn_id and request.request_id:
            if kind == "approval":
                self.turn_state.await_approval(request.thread_id, request.turn_id, request.request_id)
            elif kind == "question":
                self.turn_state.await_user_input(request.thread_id, request.turn_id, request.request_id)
        return self.render_pending_request(request)

    def _capture_item_completed(self, params: dict, store) -> OutboundMessage | None:
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            thread_id = params.get("threadId", "")
            turn_id = params.get("turnId", "")
            text = item.get("text", "")
            phase = item.get("phase")
            if phase == "final_answer" and self._is_stale_turn(thread_id, turn_id, store):
                return None
            message = self.message_pump.record_agent_message(
                thread_id=thread_id,
                turn_id=turn_id,
                phase=phase,
                text=text,
                emit_commentary=self._show_commentary(thread_id, store),
            )
            if phase == "final_answer" and message is not None and self.turn_state is not None:
                self.turn_state.mark_terminal_emitted(thread_id, turn_id)
            if message is not None:
                return self._attach_conversation(thread_id, message, store)
        elif item_type == "commandExecution":
            command = item.get("command")
            if command:
                message = self.message_pump.record_command(
                    thread_id=params.get("threadId", ""),
                    turn_id=params.get("turnId", ""),
                    command=command,
                    emit_progress=self._show_toolcalls(params.get("threadId", ""), store),
                )
                if message is not None:
                    return self._attach_conversation(params.get("threadId", ""), message, store)
        elif item_type == "fileChange":
            paths: list[str] = []
            for change in item.get("changes", []):
                path = change.get("path")
                if path:
                    paths.append(path)
            message = self.message_pump.record_file_change(
                thread_id=params.get("threadId", ""),
                turn_id=params.get("turnId", ""),
                paths=paths,
                emit_progress=self._show_toolcalls(params.get("threadId", ""), store),
            )
            if message is not None:
                return self._attach_conversation(params.get("threadId", ""), message, store)
        return None

    def _finalize_turn(self, params: dict, store) -> OutboundMessage | None:
        thread_id = params.get("threadId", "")
        turn = params.get("turn") or {}
        turn_id = turn.get("id", "")
        status = turn.get("status", "")
        if self._is_stale_turn(thread_id, turn_id, store):
            self.message_pump.discard_turn(thread_id=thread_id, turn_id=turn_id)
            return None
        if thread_id and turn_id and status:
            store.note_turn_completed(thread_id, turn_id=turn_id, status=status)
            if self.turn_state is not None:
                if status == "completed":
                    self.turn_state.mark_completed(thread_id, turn_id)
                elif status == "failed":
                    self.turn_state.mark_failed(thread_id, turn_id)
                elif status == "interrupted":
                    self.turn_state.mark_interrupted(thread_id, turn_id)
        message = self.message_pump.finalize_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
        )
        return self._attach_conversation(thread_id, message, store)

    def _find_binding(self, store, thread_id: str | None):
        if not thread_id:
            return None
        return store.find_binding_for_thread(thread_id)

    def _attach_conversation(self, thread_id: str, message: OutboundMessage, store) -> OutboundMessage | None:
        if message is None:
            return None
        binding = self._find_binding(store, thread_id) if store is not None else None
        if binding is None:
            return message if message.channel_id and message.conversation_id else None
        message.channel_id = binding.channel_id
        message.conversation_id = binding.conversation_id
        return message

    def _show_commentary(self, thread_id: str, store) -> bool:
        return self.visibility.should_emit("commentary", thread_id=thread_id, store=store)

    def _show_toolcalls(self, thread_id: str, store) -> bool:
        return self.visibility.should_emit("toolcall", thread_id=thread_id, store=store)

    def _is_stale_turn(self, thread_id: str, turn_id: str, store) -> bool:
        if self.turn_state is not None and self.turn_state.is_stale(thread_id, turn_id):
            return True
        if store is None:
            return False
        binding = self._find_binding(store, thread_id)
        if binding is None or binding.active_thread_id != thread_id:
            return False
        if binding.active_turn_id is None:
            return self._is_superseded_turn(thread_id, turn_id, store)
        return binding.active_turn_id != turn_id

    def _is_superseded_turn(self, thread_id: str, turn_id: str, store) -> bool:
        if store is None:
            return False
        try:
            thread = store.get_thread(thread_id)
        except KeyError:
            return False
        return turn_id in thread.stale_turn_ids

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

from __future__ import annotations

from collections import defaultdict

from .models import OutboundMessage, PendingRequest


class MessageProjector:
    def __init__(self) -> None:
        self._turn_messages: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._turn_commands: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._turn_files: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._emitted_turn_results: set[tuple[str, str]] = set()

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
        text = (
            f"[ticket {pending.ticket_id}] Approval needed.\n"
            f"{summary}\n"
            f"Use /approve {pending.ticket_id}, /approve-session {pending.ticket_id}, "
            f"/deny {pending.ticket_id}, or /cancel {pending.ticket_id}"
        )
        return OutboundMessage(
            channel_id=pending.channel_id,
            conversation_id=pending.conversation_id,
            message_type="approval_request",
            text=text,
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
            if final_text:
                lines = [final_text]
            else:
                lines = []
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
            key = (params.get("threadId", ""), params.get("turnId", ""))
            self._turn_messages[key].append(params.get("delta", ""))
            return None
        if method == "turn/started":
            thread_id = params.get("threadId", "")
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            status = turn.get("status")
            if thread_id and turn_id and status:
                store.note_turn_started(thread_id, turn_id=turn_id, status=status)
            return None
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
        return self.render_pending_request(request)

    def _capture_item_completed(self, params: dict, store) -> OutboundMessage | None:
        item = params.get("item") or {}
        key = (params.get("threadId", ""), params.get("turnId", ""))
        item_type = item.get("type")
        if item_type == "agentMessage":
            self._turn_messages[key] = [item.get("text", "")]
            if item.get("phase") == "final_answer" and key not in self._emitted_turn_results:
                thread_id = params.get("threadId", "")
                turn_id = params.get("turnId", "")
                if thread_id and turn_id:
                    store.note_turn_completed(thread_id, turn_id=turn_id, status="completed")
                self._emitted_turn_results.add(key)
                message = self.render_turn_completed(
                    final_text=item.get("text", ""),
                    command_summaries=list(self._turn_commands.get(key, [])),
                    changed_files=list(self._turn_files.get(key, [])),
                    failed=False,
                    interrupted=False,
                )
                return self._attach_conversation(thread_id, message, store)
        elif item_type == "commandExecution":
            command = item.get("command")
            if command:
                self._turn_commands[key].append(f"Executed `{command}`")
        elif item_type == "fileChange":
            for change in item.get("changes", []):
                path = change.get("path")
                if path:
                    self._turn_files[key].append(path)
        return None

    def _finalize_turn(self, params: dict, store) -> OutboundMessage | None:
        thread_id = params.get("threadId", "")
        turn = params.get("turn") or {}
        turn_id = turn.get("id", "")
        key = (thread_id, turn_id)
        status = turn.get("status", "")
        if thread_id and turn_id and status:
            store.note_turn_completed(thread_id, turn_id=turn_id, status=status)
        text = "\n".join(self._turn_messages.pop(key, []))
        commands = self._turn_commands.pop(key, [])
        files = self._turn_files.pop(key, [])
        if key in self._emitted_turn_results:
            self._emitted_turn_results.discard(key)
            return None
        message = self.render_turn_completed(
            final_text=text,
            command_summaries=commands,
            changed_files=files,
            failed=status == "failed",
            interrupted=status == "interrupted",
        )
        return self._attach_conversation(thread_id, message, store)

    def _find_binding(self, store, thread_id: str | None):
        if not thread_id:
            return None
        return store.find_binding_for_thread(thread_id)

    def _attach_conversation(self, thread_id: str, message: OutboundMessage, store) -> OutboundMessage | None:
        binding = self._find_binding(store, thread_id) if store is not None else None
        if binding is None:
            return message if message.channel_id and message.conversation_id else None
        message.channel_id = binding.channel_id
        message.conversation_id = binding.conversation_id
        return message

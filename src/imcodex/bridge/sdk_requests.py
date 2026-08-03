from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from imagent.contracts import (
    ApprovalRequest,
    ApprovalResponse,
    ConversationRef,
    InteractiveRequest,
    OutboundMessage,
    TextContent,
    TextFormat,
    UserInputRequest,
    UserInputResponse,
)
from imagent.controllers import RequestPresentation


_MAX_PRESENTED_REQUESTS = 256


@dataclass(frozen=True, slots=True)
class PresentedRequest:
    conversation_ref: ConversationRef
    request: InteractiveRequest


class ImcodexRequestPresenter:
    """IMCodex request wording plus bounded, non-authoritative handle routing."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: OrderedDict[tuple[ConversationRef, str], PresentedRequest] = (
            OrderedDict()
        )

    def present_request(
        self,
        request: InteractiveRequest,
        *,
        conversation_ref: ConversationRef,
        delivery_id: str,
        reply_to_message_id: str | None,
    ) -> RequestPresentation:
        self._remember(conversation_ref, request)
        text, response_supported = self._render(request)
        return RequestPresentation(
            message=OutboundMessage(
                delivery_id=delivery_id,
                conversation_ref=conversation_ref,
                content=(TextContent(text, TextFormat.MARKDOWN),),
                created_at=datetime.now(UTC),
                reply_to=reply_to_message_id,
                metadata={"request_kind": request.kind.value},
            ),
            response_supported=response_supported,
        )

    def match(
        self,
        conversation_ref: ConversationRef,
        token: str | None,
        *,
        kind: str,
    ) -> InteractiveRequest:
        with self._lock:
            candidates = [
                entry.request
                for entry in self._requests.values()
                if entry.conversation_ref == conversation_ref
                and entry.request.kind.value == kind
            ]
        if token:
            matches = [
                request
                for request in candidates
                if request.request_ref.native_request_id == token
                or request.request_ref.native_request_id.startswith(token)
            ]
        else:
            matches = candidates
        if not matches:
            raise ValueError(
                "Unknown approval request." if kind == "approval" else "Unknown question request."
            )
        if len(matches) != 1:
            raise ValueError("Multiple pending requests match; include a longer request id.")
        return matches[0]

    def approvals(self, conversation_ref: ConversationRef) -> tuple[ApprovalRequest, ...]:
        with self._lock:
            return tuple(
                entry.request
                for entry in self._requests.values()
                if entry.conversation_ref == conversation_ref
                and isinstance(entry.request, ApprovalRequest)
            )

    def resolved(self, conversation_ref: ConversationRef, request: InteractiveRequest) -> None:
        with self._lock:
            self._requests.pop(
                (conversation_ref, request.request_ref.native_request_id),
                None,
            )

    @staticmethod
    def approval_response(request: ApprovalRequest, action: str) -> ApprovalResponse:
        choice_ids = tuple(choice.choice_id for choice in request.choices)
        preferences = {
            "approve": ("accept", "acceptForSession", "grant_requested_permissions"),
            "deny": ("decline", "decline_requested_permissions", "cancel"),
            "cancel": ("cancel", "decline", "decline_requested_permissions"),
        }[action]
        choice_id = next(
            (candidate for candidate in preferences if candidate in choice_ids),
            choice_ids[0] if action == "approve" and choice_ids else "",
        )
        if not choice_id:
            raise ValueError("The approval request has no compatible response choice.")
        return ApprovalResponse(choice_id)

    @staticmethod
    def user_input_response(
        request: UserInputRequest,
        answers: dict[str, tuple[str, ...]],
    ) -> UserInputResponse:
        questions = {question.question_id: question for question in request.questions}
        normalized: dict[str, tuple[str, ...]] = {}
        for question_id, values in answers.items():
            question = questions.get(question_id)
            if question is None:
                raise ValueError(f"Unknown question id: {question_id}")
            choices = {
                choice.label.casefold(): choice.choice_id for choice in question.choices
            }
            normalized[question_id] = tuple(
                value
                if value in {choice.choice_id for choice in question.choices}
                else choices.get(value.casefold(), value)
                for value in values
            )
        return UserInputResponse(normalized)

    def _remember(self, conversation_ref, request) -> None:
        key = (conversation_ref, request.request_ref.native_request_id)
        with self._lock:
            self._requests.pop(key, None)
            self._requests[key] = PresentedRequest(conversation_ref, request)
            while len(self._requests) > _MAX_PRESENTED_REQUESTS:
                self._requests.popitem(last=False)

    @staticmethod
    def _render(request: InteractiveRequest) -> tuple[str, bool]:
        handle = request.request_ref.native_request_id[:8]
        native_id = request.request_ref.native_request_id
        if isinstance(request, ApprovalRequest):
            lines = [
                f"[request {handle}] Approval needed.",
                f"Native request id: {native_id}",
                request.prompt,
                "Use /approve to allow, /deny to reject, or send a new message to cancel and continue.",
                f"Target one request with /approve {handle}, /deny {handle}, or /cancel {handle}.",
            ]
            return "\n".join(line for line in lines if line), True
        if any(question.secret for question in request.questions):
            return (
                "Secure input is required. Respond in Codex or another explicitly secure client.",
                False,
            )
        lines = [
            f"[request {handle}] Codex needs more input.",
            f"Native request id: {native_id}",
        ]
        for question in request.questions:
            lines.append(f"- {question.question_id}: {question.prompt}")
        first = request.questions[0].question_id if request.questions else "key"
        lines.append(f"Reply with /answer {native_id} {first}=value")
        return "\n".join(lines), True

from __future__ import annotations

import pytest
from imagent.applications import ApplicationRef, ProjectRef, ThreadRef, TurnRef
from imagent.applications.requests import (
    ApprovalRequest,
    RequestChoice,
    RequestRef,
    UserInputQuestion,
    UserInputRequest,
)
from imagent.interaction.messages import ConversationRef

from imcodex.bridge.sdk_requests import ImcodexRequestPresenter


APPLICATION = ApplicationRef("codex-main")
PROJECT = ProjectRef("codex-main", "imcodex-workspace")
THREAD = ThreadRef(PROJECT, "thread-1")
TURN = TurnRef(THREAD, "turn-1")
CONVERSATION = ConversationRef("telegram", "chat-1")


def _approval(native_id: str) -> ApprovalRequest:
    return ApprovalRequest(
        RequestRef(APPLICATION, native_id),
        TURN,
        "Allow command?",
        (
            RequestChoice("accept", "Approve"),
            RequestChoice("decline", "Deny"),
            RequestChoice("cancel", "Cancel"),
        ),
    )


def _question(native_id: str, *, secret: bool = False) -> UserInputRequest:
    return UserInputRequest(
        RequestRef(APPLICATION, native_id),
        TURN,
        (
            UserInputQuestion(
                "environment",
                "Choose environment",
                choices=(
                    RequestChoice("staging", "Staging"),
                    RequestChoice("production", "Production"),
                ),
                secret=secret,
            ),
        ),
        prompt="Deployment input",
    )


def _present(presenter: ImcodexRequestPresenter, request):
    return presenter.present_request(
        request,
        conversation_ref=CONVERSATION,
        delivery_id=f"delivery:{request.request_ref.native_request_id}",
        reply_to_message_id="message-1",
    )


def test_approval_presentation_preserves_native_identity_and_choices() -> None:
    presenter = ImcodexRequestPresenter()
    request = _approval("approval-123456789")

    presentation = _present(presenter, request)

    text = presentation.message.content[0].text
    assert presentation.response_supported is True
    assert presentation.message.metadata["request_kind"] == "approval"
    assert "approval-123456789" in text
    assert "/approve approval" in text
    assert presenter.approval_response(request, "approve").choice_id == "accept"
    assert presenter.approval_response(request, "deny").choice_id == "decline"
    assert presenter.approval_response(request, "cancel").choice_id == "cancel"


def test_ambiguous_request_prefix_requires_a_longer_native_handle() -> None:
    presenter = ImcodexRequestPresenter()
    _present(presenter, _approval("same-prefix-one"))
    _present(presenter, _approval("same-prefix-two"))

    with pytest.raises(ValueError, match="Multiple pending requests"):
        presenter.match(CONVERSATION, "same-prefix", kind="approval")

    matched = presenter.match(CONVERSATION, "same-prefix-o", kind="approval")
    assert matched.request_ref.native_request_id == "same-prefix-one"


def test_secret_question_is_not_answerable_through_im() -> None:
    presentation = _present(ImcodexRequestPresenter(), _question("secret-1", secret=True))

    assert presentation.response_supported is False
    assert "Secure input is required" in presentation.message.content[0].text
    assert "/answer" not in presentation.message.content[0].text


def test_question_answers_map_labels_to_public_choice_ids() -> None:
    presenter = ImcodexRequestPresenter()
    request = _question("question-1")
    _present(presenter, request)

    response = presenter.user_input_response(
        request,
        {"environment": ("Production",)},
    )

    assert response.answers == {"environment": ("production",)}


def test_resolved_request_is_removed_and_restart_requires_sdk_replay() -> None:
    presenter = ImcodexRequestPresenter()
    request = _approval("approval-replay-1")
    _present(presenter, request)
    presenter.resolved(CONVERSATION, request)

    with pytest.raises(ValueError, match="Unknown approval"):
        presenter.match(CONVERSATION, None, kind="approval")

    restarted = ImcodexRequestPresenter()
    with pytest.raises(ValueError, match="Unknown approval"):
        restarted.match(CONVERSATION, None, kind="approval")
    _present(restarted, request)
    assert restarted.match(CONVERSATION, None, kind="approval") == request

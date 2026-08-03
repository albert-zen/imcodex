from __future__ import annotations

from datetime import UTC, datetime

from imagent.contracts import (
    ApplicationRef,
    ApprovalRequest,
    ConversationRef,
    RequestChoice,
    RequestRef,
    ThreadRef,
    UserInputQuestion,
    UserInputRequest,
)

from imcodex.bridge.sdk_requests import ImcodexRequestPresenter


def _approval() -> ApprovalRequest:
    return ApprovalRequest(
        request_ref=RequestRef(ApplicationRef("codex-main"), "appserver:sha256:abcdef"),
        thread_ref=ThreadRef("codex-main", "thread-1"),
        turn_id="turn-1",
        prompt="Run the command?",
        choices=(RequestChoice("accept", "Approve"), RequestChoice("decline", "Deny")),
    )


def test_request_presenter_preserves_product_approval_grammar() -> None:
    presenter = ImcodexRequestPresenter()
    conversation = ConversationRef("telegram", "chat-1")
    request = _approval()

    presentation = presenter.present_request(
        request,
        conversation_ref=conversation,
        delivery_id="delivery-1",
        reply_to_message_id="message-1",
    )

    text = presentation.message.content[0].text
    assert "Approval needed." in text
    assert "Use /approve to allow, /deny to reject" in text
    assert presenter.match(conversation, "appserver", kind="approval") is request
    assert presenter.approval_response(request, "approve").choice_id == "accept"


def test_user_input_labels_map_back_to_typed_choice_ids() -> None:
    presenter = ImcodexRequestPresenter()
    request = UserInputRequest(
        request_ref=RequestRef(ApplicationRef("codex-main"), "request-input"),
        thread_ref=ThreadRef("codex-main", "thread-1"),
        turn_id="turn-1",
        questions=(
            UserInputQuestion(
                question_id="color",
                prompt="Choose a color",
                choices=(RequestChoice("option:1", "Blue"),),
                allows_other=False,
            ),
        ),
    )

    response = presenter.user_input_response(request, {"color": ("Blue",)})

    assert response.answers == {"color": ("option:1",)}

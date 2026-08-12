from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from imagent.applications import ApplicationRef, ProjectRef, ThreadRef, TurnRef
from imagent.applications.requests import ApprovalRequest, RequestChoice, RequestRef
from imagent.gateway.outcomes import Failed, Succeeded
from imagent.interaction.messages import ConversationRef, InboundMessage, TextContent

from imcodex.bridge.sdk_controller import ImcodexController
from imcodex.bridge.sdk_requests import ImcodexRequestPresenter
from imcodex.product_state import ProductState


class _Client:
    def __init__(self, config=None) -> None:
        self.config = config or {}
        self.batch_writes = []

    async def read_config(self):
        return self.config

    async def batch_write_config(self, **kwargs):
        self.batch_writes.append(kwargs)
        return {"status": "ok"}


class _Actions:
    def __init__(self) -> None:
        self.binding = None
        self.calls = []

    async def get_binding(self):
        return self.binding

    async def _enter_effectful_command(self, invocation):
        self.calls.append(("effectful_fence", invocation.invocation_id))

    async def select_application(self, ref, *, action_id):
        self.calls.append(("select_application", action_id, ref))
        self.binding = SimpleNamespace(
            application_ref=ref,
            project_ref=None,
            thread_ref=None,
        )
        return Succeeded(self.binding)

    async def select_project(self, ref, *, action_id):
        self.calls.append(("select_project", action_id, ref))
        self.binding = SimpleNamespace(
            application_ref=ApplicationRef(ref.application_instance_id),
            project_ref=ref,
            thread_ref=None,
        )
        return Succeeded(self.binding)

    async def create_and_bind_thread(self, ref, *, action_id, title):
        self.calls.append(("create_thread", action_id, ref, title))
        thread_ref = ThreadRef(ref, "thread-1")
        self.binding = SimpleNamespace(
            application_ref=ApplicationRef(ref.application_instance_id),
            project_ref=ref,
            thread_ref=thread_ref,
        )
        return Succeeded(SimpleNamespace(ref=thread_ref))

    async def observe_thread(self, ref, *, action_id, reply_to_message_id):
        self.calls.append(("observe_thread", action_id, ref, reply_to_message_id))
        return Succeeded(None)

    async def interrupt_turn(self, ref, *, action_id):
        self.calls.append(("interrupt_turn", action_id, ref))
        return Succeeded(None)

    async def respond_request(self, ref, response, *, action_id):
        self.calls.append(("respond_request", action_id, ref, response))
        return Succeeded(None)


def _message(text: str, message_id: str = "message-1") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        conversation_ref=ConversationRef("telegram", "chat-1"),
        sender="user-1",
        content=(TextContent(text),),
        created_at=datetime.now(UTC),
    )


def _invocation(*arguments: str, command_name: str = "view"):
    return SimpleNamespace(
        arguments=arguments,
        command_name=command_name,
        conversation_ref=ConversationRef("telegram", "chat-1"),
    )


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("minimal", (False, False, False)),
        ("standard", (True, False, False)),
        ("verbose", (True, True, True)),
    ],
)
async def test_view_profile_updates_effective_visibility_flags(
    tmp_path, profile: str, expected: tuple[bool, bool, bool]
) -> None:
    state = ProductState(tmp_path / "product.json")
    controller = ImcodexController(client=_Client(), product_state=state)

    await controller._view(_invocation(profile), None)

    saved = state.get("telegram", "chat-1")
    assert saved["visibility"] == profile
    assert (
        saved["show_commentary"],
        saved["show_toolcalls"],
        saved["show_system"],
    ) == expected


@pytest.mark.parametrize(
    ("mode", "approval", "sandbox"),
    [
        ("default", "on-request", "workspace-write"),
        ("read-only", "on-request", "read-only"),
        ("full-access", "never", "danger-full-access"),
    ],
)
async def test_permission_writes_approval_and_sandbox_atomically(
    tmp_path, mode: str, approval: str, sandbox: str
) -> None:
    client = _Client()
    controller = ImcodexController(
        client=client,
        product_state=ProductState(tmp_path / "product.json"),
    )

    await controller._permission(_invocation(mode, command_name="permission"), None)

    assert client.batch_writes == [
        {
            "edits": [
                {
                    "keyPath": "approval_policy",
                    "value": approval,
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "sandbox_mode",
                    "value": sandbox,
                    "mergeStrategy": "replace",
                },
            ],
            "reload_user_config": True,
        }
    ]


async def test_permission_readback_reports_effective_pair(tmp_path) -> None:
    client = _Client(
        {
            "config": {
                "approvalPolicy": "never",
                "sandboxMode": {"mode": "danger-full-access"},
            }
        }
    )
    controller = ImcodexController(
        client=client,
        product_state=ProductState(tmp_path / "product.json"),
    )

    result = await controller._permission(_invocation(command_name="permission"), None)

    assert "Permission mode: `full-access`" in result.content[0].text
    assert "sandbox: `danger-full-access`" in result.content[0].text


async def test_first_ordinary_input_selects_workspace_and_observes_new_thread(tmp_path) -> None:
    actions = _Actions()
    controller = ImcodexController(
        client=_Client(),
        product_state=ProductState(tmp_path / "product.json"),
    )

    result = await controller.handle(_message("hello", "inbound-7"), actions)

    assert result is None
    assert [call[0] for call in actions.calls] == [
        "select_application",
        "select_project",
        "create_thread",
        "observe_thread",
    ]
    assert [call[1] for call in actions.calls] == [
        "imcodex:inbound-7:application.select",
        "imcodex:inbound-7:project.select",
        "imcodex:inbound-7:thread.create",
        "imcodex:inbound-7:thread.observe",
    ]


async def test_effectful_command_uses_registry_invocation_for_stable_action_id(tmp_path) -> None:
    actions = _Actions()
    project_ref = ProjectRef("codex-main", "imcodex-workspace")
    actions.binding = SimpleNamespace(
        application_ref=ApplicationRef("codex-main"),
        project_ref=project_ref,
        thread_ref=ThreadRef(project_ref, "thread-9"),
    )
    controller = ImcodexController(
        client=_Client(),
        product_state=ProductState(tmp_path / "product.json"),
    )

    first = await controller.handle(_message("/stop", "stop-4"), actions)
    action_id = actions.calls[-1][1]
    second = await controller.handle(_message("/stop", "stop-4"), actions)

    assert first and "interrupted" in first[0].content[0].text
    assert second and actions.calls[-1][1] == action_id
    assert action_id.startswith("imcodex:")
    assert action_id.endswith(":turn.interrupt")


async def test_command_failure_is_returned_as_product_error(tmp_path) -> None:
    actions = _Actions()

    async def reject(*_args, **_kwargs):
        return Failed(RuntimeError("native operation rejected"))

    actions.select_application = reject
    controller = ImcodexController(
        client=_Client(),
        product_state=ProductState(tmp_path / "product.json"),
    )

    output = await controller.handle(_message("/threads", "failed-1"), actions)

    assert output and output[0].metadata["imcodex_product_controller"] is True
    assert output[0].reply_to == "failed-1"
    assert output[0].content[0].text == "**Error:** native operation rejected"
    assert "Traceback" not in output[0].content[0].text


async def test_plain_input_cancels_pending_approvals_before_continuing(tmp_path) -> None:
    actions = _Actions()
    project_ref = ProjectRef("codex-main", "imcodex-workspace")
    thread_ref = ThreadRef(project_ref, "thread-1")
    actions.binding = SimpleNamespace(
        application_ref=ApplicationRef("codex-main"),
        project_ref=project_ref,
        thread_ref=thread_ref,
    )
    presenter = ImcodexRequestPresenter()
    for native_id in ("approval-one", "approval-two"):
        presenter.present_request(
            ApprovalRequest(
                RequestRef(ApplicationRef("codex-main"), native_id),
                TurnRef(thread_ref, "turn-1"),
                "Allow?",
                (RequestChoice("cancel", "Cancel"),),
            ),
            conversation_ref=ConversationRef("telegram", "chat-1"),
            delivery_id=f"delivery:{native_id}",
            reply_to_message_id="request-message",
        )
    controller = ImcodexController(
        client=_Client(),
        product_state=ProductState(tmp_path / "product.json"),
        request_presenter=presenter,
    )

    assert await controller.handle(_message("new direction", "steer-1"), actions) is None

    responses = [call for call in actions.calls if call[0] == "respond_request"]
    assert [call[2].native_request_id for call in responses] == [
        "approval-one",
        "approval-two",
    ]
    assert all(call[3].choice_id == "cancel" for call in responses)
    assert [call[1] for call in responses] == [
        "imcodex:steer-1:request.cancel:approval-one",
        "imcodex:steer-1:request.cancel:approval-two",
    ]
    assert presenter.approvals(ConversationRef("telegram", "chat-1")) == ()

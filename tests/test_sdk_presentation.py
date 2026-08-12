from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from imagent.interaction.messages import ConversationRef, OutboundMessage, TextContent

from imcodex.bridge.sdk_controller import ImcodexController
from imcodex.bridge.sdk_presentation import ImcodexOutboundPresentation
from imcodex.product_state import ProductState


def _invocation(profile: str):
    return SimpleNamespace(
        arguments=(profile,),
        command_name="view",
        conversation_ref=ConversationRef("telegram", "chat-1"),
    )


def _message(delivery_id: str, **metadata) -> OutboundMessage:
    return OutboundMessage(
        delivery_id=delivery_id,
        conversation_ref=ConversationRef("telegram", "chat-1"),
        content=(TextContent("output"),),
        created_at=datetime.now(UTC),
        metadata=metadata,
    )


@pytest.mark.parametrize(
    ("profile", "commentary", "toolcall", "system"),
    [
        ("minimal", False, False, False),
        ("standard", True, False, False),
        ("verbose", True, True, True),
    ],
)
async def test_view_profile_controls_presentation(
    tmp_path,
    profile: str,
    commentary: bool,
    toolcall: bool,
    system: bool,
) -> None:
    state = ProductState(tmp_path / "product.json")
    controller = ImcodexController(client=SimpleNamespace(), product_state=state)
    presentation = ImcodexOutboundPresentation(product_state=state)
    await controller._view(_invocation(profile), None)

    outputs = [
        await presentation.present(_message("commentary", phase="commentary"), None),
        await presentation.present(_message("tool", kind="command_execution"), None),
        await presentation.present(_message("system", live_only=True), None),
    ]

    assert tuple(item is not None for item in outputs) == (
        commentary,
        toolcall,
        system,
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"phase": "final_answer"},
        {"request_kind": "approval"},
        {"request_kind": "user_input"},
        {"imcodex_product_controller": True},
        {"kind": "artifact_materialization"},
        {"kind": "artifact_terminal_fallback"},
    ],
)
async def test_required_output_is_visible_even_in_minimal_profile(
    tmp_path,
    metadata,
) -> None:
    state = ProductState(tmp_path / "product.json")
    controller = ImcodexController(client=SimpleNamespace(), product_state=state)
    presentation = ImcodexOutboundPresentation(product_state=state)
    await controller._view(_invocation("minimal"), None)

    assert await presentation.present(_message("required", **metadata), None) is not None


async def test_show_and_hide_override_one_visibility_class(tmp_path) -> None:
    state = ProductState(tmp_path / "product.json")
    controller = ImcodexController(client=SimpleNamespace(), product_state=state)
    presentation = ImcodexOutboundPresentation(product_state=state)
    await controller._view(_invocation("standard"), None)

    await controller._show_hide(
        SimpleNamespace(
            arguments=("toolcalls",),
            command_name="show",
            conversation_ref=ConversationRef("telegram", "chat-1"),
        ),
        None,
    )
    shown = await presentation.present(_message("tool", kind="file_change"), None)
    await controller._show_hide(
        SimpleNamespace(
            arguments=("commentary",),
            command_name="hide",
            conversation_ref=ConversationRef("telegram", "chat-1"),
        ),
        None,
    )
    hidden = await presentation.present(_message("comment", phase="commentary"), None)

    assert shown is not None
    assert hidden is None

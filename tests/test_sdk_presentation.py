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

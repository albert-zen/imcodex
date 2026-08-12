from __future__ import annotations

from types import SimpleNamespace

import pytest
from imagent.interaction.messages import ConversationRef

from imcodex.bridge.sdk_controller import ImcodexController
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

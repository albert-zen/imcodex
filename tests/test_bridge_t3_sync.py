from __future__ import annotations

import json

import pytest

from imcodex.appserver.thread_observer import NativeThreadObserverError
from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.models import InboundMessage
from imcodex.store import ConversationStore


class FailingObserverBackend:
    supports_local_image_paths = False

    async def submit_input(self, *args, **kwargs):
        raise NativeThreadObserverError("request_timeout")


@pytest.mark.asyncio
async def test_bridge_reports_fail_closed_t3_sync_without_claiming_codex_accepted(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.bind_thread_with_cwd("qq", "conv", "native-1", "/work/repo")
    service = BridgeService(
        store=store,
        backend=FailingObserverBackend(),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    result = await service.handle_inbound(
        InboundMessage("qq", "conv", "user", "message-1", "hello")
    )
    await store.flush_pending_writes()

    assert len(result) == 1
    assert result[0].message_type == "error"
    assert "Codex did not receive this message" in result[0].text
    persisted = state_path.read_text(encoding="utf-8")
    assert "project-1" not in persisted
    assert "providerInstanceId" not in persisted
    assert "t3" not in json.loads(persisted)["bindings"][0]

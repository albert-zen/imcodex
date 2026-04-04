from __future__ import annotations

import pytest

from imcodex.commands import CommandRouter
from imcodex.models import InboundMessage
from imcodex.projector import MessageProjector
from imcodex.service import BridgeService
from imcodex.store import ConversationStore


class FakeBackend:
    def __init__(self) -> None:
        self.created_threads: list[tuple[str, str]] = []
        self.ensure_threads: list[tuple[str, str]] = []
        self.started_turns: list[tuple[str, str, str]] = []
        self.interrupts: list[tuple[str, str]] = []
        self.replies: list[tuple[str, dict]] = []

    async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
        self.created_threads.append((channel_id, conversation_id))
        return "thr_remote_new"

    async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
        self.ensure_threads.append((channel_id, conversation_id))
        return "thr_existing"

    async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
        self.started_turns.append((channel_id, conversation_id, text))
        return "turn_1"

    async def interrupt_active_turn(self, channel_id: str, conversation_id: str) -> None:
        self.interrupts.append((channel_id, conversation_id))

    async def reply_to_server_request(self, ticket_id: str, decision_or_answers: dict) -> None:
        self.replies.append((ticket_id, decision_or_answers))


@pytest.mark.asyncio
async def test_plain_text_uses_single_discovered_project_and_starts_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    backend = FakeBackend()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="please inspect the repo",
        )
    )

    assert backend.ensure_threads == [("qq", "conv-1")]
    assert backend.started_turns == [("qq", "conv-1", "please inspect the repo")]
    assert [message.message_type for message in messages] == ["accepted", "processing"]


@pytest.mark.asyncio
async def test_new_command_calls_backend_thread_creation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_project("qq", "conv-1", thread.project_id)
    backend = FakeBackend()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/new",
        )
    )

    assert backend.created_threads == [("qq", "conv-1")]
    assert "thr_remote_new" in messages[0].text


@pytest.mark.asyncio
async def test_approval_command_replies_before_pending_is_removed() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="7",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        request_id="99",
        request_method="item/commandExecution/requestApproval",
    )
    backend = FakeBackend()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve 7",
        )
    )

    assert backend.replies == [("7", {"decision": "accept"})]
    assert store.get_pending_request("7") is not None

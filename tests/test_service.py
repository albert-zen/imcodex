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

    assert backend.ensure_threads == []
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


@pytest.mark.asyncio
async def test_plain_text_without_project_mentions_cwd_command() -> None:
    store = ConversationStore(clock=lambda: 1.0)
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

    assert messages[0].message_type == "error"
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_server_approval_request_can_be_auto_approved_without_prompt() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("qq", "conv-1", "thr_seed")
    backend = FakeBackend()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        auto_approve_mode="acceptForSession",
    )

    messages = await service.handle_server_request(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_seed",
                "turnId": "turn_1",
                "command": "pytest -q",
                "_request_id": "99",
            },
        }
    )

    assert messages == []
    assert backend.replies == [("1", {"decision": "acceptForSession"})]


@pytest.mark.asyncio
async def test_plain_text_accepted_message_uses_recovered_thread_id() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class ReplacingBackend(FakeBackend):
        async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
            self.ensure_threads.append((channel_id, conversation_id))
            store.get_binding(channel_id, conversation_id).active_thread_id = "thr_old"
            return "thr_old"

        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            store.get_binding(channel_id, conversation_id).active_thread_id = "thr_new"
            return "turn_1"

    backend = ReplacingBackend()
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

    assert messages[0].text == "Accepted for thread thr_new."

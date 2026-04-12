from __future__ import annotations

from pathlib import Path

import pytest

from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.models import InboundMessage
from imcodex.store import ConversationStore


class FakeBackend:
    def __init__(self) -> None:
        self.created_threads: list[tuple[str, str]] = []
        self.attached_threads: list[tuple[str, str, str]] = []
        self.ensure_threads: list[tuple[str, str]] = []
        self.started_turns: list[tuple[str, str, str]] = []
        self.interrupts: list[tuple[str, str]] = []
        self.replies: list[tuple[str, dict]] = []

    async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
        self.created_threads.append((channel_id, conversation_id))
        return "thr_remote_new"

    async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
        self.attached_threads.append((channel_id, conversation_id, thread_id))
        return thread_id

    async def ensure_thread(self, channel_id: str, conversation_id: str) -> str:
        self.ensure_threads.append((channel_id, conversation_id))
        return "thr_existing"

    async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
        self.started_turns.append((channel_id, conversation_id, text))
        return "turn_1"

    async def interrupt_active_turn(self, channel_id: str, conversation_id: str) -> None:
        self.interrupts.append((channel_id, conversation_id))

    async def reply_to_server_request(
        self,
        channel_id: str,
        conversation_id: str,
        ticket_id: str,
        decision_or_answers: dict,
    ) -> None:
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
    assert [message.message_type for message in messages] == ["accepted"]
    assert messages[0].text == "Working on it."


@pytest.mark.asyncio
async def test_new_command_calls_backend_thread_creation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_project("qq", "conv-1", thread.project_id)

    class RecordingBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=thread.cwd, preview="Fresh repo check")
            store.set_active_thread(channel_id, conversation_id, "thr_remote_new")
            return "thr_remote_new"

    backend = RecordingBackend()
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
    assert "Fresh repo check" in messages[0].text
    assert "thr_remote_new" in messages[0].text
    assert messages[0].text.index("Fresh repo check") < messages[0].text.index("thr_remote_new")


@pytest.mark.asyncio
async def test_new_command_followed_by_first_prompt_sets_fallback_thread_label() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class RecordingBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=project.cwd, preview="")
            store.set_active_thread(channel_id, conversation_id, "thr_remote_new")
            return "thr_remote_new"

    backend = RecordingBackend()
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
            text="/new",
        )
    )

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="please inspect why the Windows working directory resets after restart",
        )
    )

    assert messages[0].text == "Working on it."
    assert (
        store.thread_label("thr_remote_new")
        == "please inspect why the Windows working directory resets..."
    )


@pytest.mark.asyncio
async def test_thread_attach_calls_backend_and_reports_canonical_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=project.cwd, preview="External session")
            store.set_active_thread(channel_id, conversation_id, "thr_attached")
            return "thr_attached"

    backend = AttachingBackend()
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
            text="/thread attach thr_external",
        )
    )

    assert backend.attached_threads == [("qq", "conv-1", "thr_external")]
    assert messages[0].text == "Attached thread External session (id: thr_attached)."


@pytest.mark.asyncio
async def test_thread_attach_refreshes_label_for_known_previewless_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)
    store.record_thread("thr_known", cwd=project.cwd, preview="")

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_known", cwd=project.cwd, preview="Imported thread")
            store.set_active_thread(channel_id, conversation_id, "thr_known")
            return "thr_known"

    backend = AttachingBackend()
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
            text="/thread attach thr_known",
        )
    )

    assert messages[0].text == "Attached thread Imported thread (id: thr_known)."


@pytest.mark.asyncio
async def test_new_command_does_not_require_backend_to_store_thread_before_ack() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)
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
    assert messages[0].text == "Started new thread Untitled thread (id: thr_remote_new)."


@pytest.mark.asyncio
async def test_new_command_without_working_directory_is_user_safe() -> None:
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
            text="/new",
        )
    )

    assert backend.created_threads == []
    assert messages[0].message_type == "error"
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_new_command_pending_label_survives_restart_before_first_prompt(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class CreatingBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=project.cwd, preview="")
            store.set_active_thread(channel_id, conversation_id, "thr_remote_new")
            return "thr_remote_new"

    create_backend = CreatingBackend()
    create_service = BridgeService(
        store=store,
        backend=create_backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    await create_service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/new",
        )
    )

    reloaded_store = ConversationStore(clock=lambda: 2.0, state_path=state_path)

    class RestartedBackend(FakeBackend):
        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            reloaded_store.set_active_turn(
                channel_id,
                conversation_id,
                thread_id="thr_remote_new",
                turn_id="turn_1",
                status="inProgress",
            )
            return "turn_1"

    restarted_backend = RestartedBackend()
    restarted_service = BridgeService(
        store=reloaded_store,
        backend=restarted_backend,
        command_router=CommandRouter(reloaded_store),
        projector=MessageProjector(),
    )

    messages = await restarted_service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="please inspect why the Windows working directory resets after restart",
        )
    )

    assert messages[0].text == "Working on it."
    assert (
        reloaded_store.thread_label("thr_remote_new")
        == "please inspect why the Windows working directory resets..."
    )


@pytest.mark.asyncio
async def test_new_command_recovery_clears_stale_pending_label_from_abandoned_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class RecoveringBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=project.cwd, preview="")
            store.set_active_thread(channel_id, conversation_id, "thr_remote_new")
            return "thr_remote_new"

        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            if text.startswith("please inspect"):
                store.record_thread("thr_recovered", cwd=project.cwd, preview="")
                store.set_active_turn(
                    channel_id,
                    conversation_id,
                    thread_id="thr_recovered",
                    turn_id="turn_1",
                    status="inProgress",
                )
                return "turn_1"
            active_thread_id = store.get_binding(channel_id, conversation_id).active_thread_id or "thr_remote_new"
            store.set_active_turn(
                channel_id,
                conversation_id,
                thread_id=active_thread_id,
                turn_id="turn_2",
                status="inProgress",
            )
            return "turn_2"

    backend = RecoveringBackend()
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
            text="/new",
        )
    )
    await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="please inspect why the Windows working directory resets after restart",
        )
    )

    assert (
        store.thread_label("thr_recovered")
        == "please inspect why the Windows working directory resets..."
    )

    store.set_active_thread("qq", "conv-1", "thr_remote_new")
    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m3",
            text="continue",
        )
    )

    assert messages[0].text == "Working on it."
    assert store.thread_label("thr_remote_new") == "Untitled thread"


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
async def test_batch_approval_replies_to_each_known_ticket() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        request_id="91",
        request_method="item/commandExecution/requestApproval",
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Approve another command",
        payload={"command": "git diff"},
        request_id="92",
        request_method="item/commandExecution/requestApproval",
    )
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
            text="/approve 1 2 9",
        )
    )

    assert [message.message_type for message in messages] == ["command_result"]
    assert "Unknown tickets: 9." in messages[0].text
    assert backend.replies == [
        ("1", {"decision": "accept"}),
        ("2", {"decision": "accept"}),
    ]
    assert store.get_pending_request("1") is not None
    assert store.get_pending_request("2") is not None


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
    assert "working directory" in messages[0].text.lower()


@pytest.mark.asyncio
async def test_server_approval_request_can_be_auto_approved_without_prompt() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("qq", "conv-1", "thr_seed")
    store.set_permission_profile("qq", "conv-1", "autonomous")
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
async def test_review_permission_mode_blocks_env_auto_approval() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("qq", "conv-1", "thr_seed")
    store.set_permission_profile("qq", "conv-1", "review")
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

    assert [message.message_type for message in messages] == ["approval_request"]
    assert backend.replies == []


@pytest.mark.asyncio
async def test_plain_text_records_first_user_message_for_future_thread_labels() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    project = store.ensure_project(r"D:\work\alpha")
    store.set_active_project("qq", "conv-1", project.project_id)

    class RecordingBackend(FakeBackend):
        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            store.record_thread("thr_new", cwd=project.cwd, preview="")
            store.get_binding(channel_id, conversation_id).active_thread_id = "thr_new"
            return "turn_1"

    backend = RecordingBackend()
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
            text="please inspect why the Windows working directory resets after restart",
        )
    )

    assert messages[0].text == "Working on it."
    assert (
        store.thread_label("thr_new")
        == "please inspect why the Windows working directory resets..."
    )


@pytest.mark.asyncio
async def test_plain_text_does_not_retitle_existing_previewless_thread_on_follow_up() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_existing", cwd=r"D:\work\alpha", preview="")
    store.set_active_thread("qq", "conv-1", thread.thread_id)

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
            text="continue",
        )
    )

    assert messages[0].text == "Working on it."
    assert store.thread_label("thr_existing") == "Untitled thread"

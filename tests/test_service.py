from __future__ import annotations

import logging
from pathlib import Path

import pytest

from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.bridge.request_registry import RequestRegistry
from imcodex.bridge.session_registry import SessionRegistry
from imcodex.bridge.turn_state import TurnStateMachine
from imcodex.bridge.visibility import VisibilityClassifier
from imcodex.appserver import StaleThreadBindingError
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
        self.list_threads_calls: list[tuple[str, str, bool]] = []
        self.read_thread_calls: list[tuple[str, str, str]] = []
        self.list_threads_result: list[object] = []
        self.read_thread_result: object | None = None
        self.fail_list_threads = False
        self.fail_read_thread = False

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

    async def list_threads(self, channel_id: str, conversation_id: str, include_all: bool = False):
        self.list_threads_calls.append((channel_id, conversation_id, include_all))
        if self.fail_list_threads:
            raise RuntimeError("thread/list unavailable")
        return list(self.list_threads_result)

    async def read_thread(self, channel_id: str, conversation_id: str, thread_id: str):
        self.read_thread_calls.append((channel_id, conversation_id, thread_id))
        if self.fail_read_thread:
            raise RuntimeError("thread/read unavailable")
        return self.read_thread_result


@pytest.mark.asyncio
async def test_plain_text_requires_explicit_working_directory_even_with_single_cached_project() -> None:
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
    assert backend.started_turns == []
    assert [message.message_type for message in messages] == ["error"]
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_plain_text_does_not_use_legacy_project_alias_without_selected_cwd() -> None:
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

    assert backend.ensure_threads == []
    assert backend.started_turns == []
    assert [message.message_type for message in messages] == ["error"]
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_new_command_calls_backend_thread_creation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_seed", cwd=r"D:\work\alpha", preview="seed")
    store.set_selected_cwd("qq", "conv-1", thread.cwd)

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
async def test_new_command_followed_by_first_prompt_keeps_native_thread_identity_plain() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class RecordingBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=cwd, preview="")
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

    assert messages[0].text == "[System] Accepted. Processing started."
    assert store.thread_label("thr_remote_new") == "Untitled thread"


@pytest.mark.asyncio
async def test_thread_attach_calls_backend_and_reports_canonical_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=cwd, preview="External session")
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
    assert messages[0].text == "[System] Attached to thread External session (id: thr_attached)."


@pytest.mark.asyncio
async def test_runtime_session_index_routes_server_request_to_latest_attached_conversation() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=cwd, preview="External session")
            store.set_active_thread(channel_id, conversation_id, "thr_attached")
            return "thr_attached"

    backend = AttachingBackend()
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
            text="/thread attach thr_external",
        )
    )
    await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-2",
            user_id="u2",
            message_id="m2",
            text="/thread attach thr_external",
        )
    )

    messages = await service.handle_server_request(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_attached",
                "turnId": "turn_1",
                "_request_id": "99",
                "command": "pytest -q",
            },
        }
    )

    assert [message.message_type for message in messages] == ["approval_request"]
    assert messages[0].channel_id == "qq"
    assert messages[0].conversation_id == "conv-2"


@pytest.mark.asyncio
async def test_read_only_command_does_not_steal_runtime_thread_owner() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=cwd, preview="External session")
            store.set_active_thread(channel_id, conversation_id, "thr_attached")
            return "thr_attached"

    backend = AttachingBackend()
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
            text="/thread attach thr_external",
        )
    )
    await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-2",
            user_id="u2",
            message_id="m2",
            text="/thread attach thr_external",
        )
    )

    status_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m3",
            text="/status",
        )
    )
    assert status_messages[0].message_type == "command_result"

    messages = await service.handle_server_request(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_attached",
                "turnId": "turn_1",
                "_request_id": "99",
                "command": "pytest -q",
            },
        }
    )

    assert messages[0].conversation_id == "conv-2"


@pytest.mark.asyncio
async def test_thread_attach_can_resume_without_preselected_working_directory() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=r"D:\work\alpha", preview="External session")
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
    assert messages[0].text == "[System] Attached to thread External session (id: thr_attached)."


@pytest.mark.asyncio
async def test_plain_text_after_attach_without_preselected_cwd_uses_attached_thread_workspace() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_attached", cwd=r"D:\work\alpha", preview="External session")
            store.set_active_thread(channel_id, conversation_id, "thr_attached")
            return "thr_attached"

    backend = AttachingBackend()
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
            text="/thread attach thr_external",
        )
    )
    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="continue from the attached thread",
        )
    )

    assert backend.started_turns == [("qq", "conv-1", "continue from the attached thread")]
    assert messages[0].message_type == "accepted"
    assert messages[0].text == "[System] Accepted. Processing started."


@pytest.mark.asyncio
async def test_thread_attach_reports_backend_validation_failure_without_breaking_command_flow() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    class FailingAttachBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            raise RuntimeError("thread thr_external did not provide a working directory")

    backend = FailingAttachBackend()
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

    assert messages[0].message_type == "status"
    assert "could not be attached" in messages[0].text
    assert "/thread read <thread-id>" in messages[0].text


@pytest.mark.asyncio
async def test_thread_attach_refreshes_label_for_known_previewless_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)
    store.record_thread("thr_known", cwd=cwd, preview="")

    class AttachingBackend(FakeBackend):
        async def attach_thread(self, channel_id: str, conversation_id: str, thread_id: str) -> str:
            self.attached_threads.append((channel_id, conversation_id, thread_id))
            store.record_thread("thr_known", cwd=cwd, preview="Imported thread")
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

    assert messages[0].text == "[System] Attached to thread Imported thread (id: thr_known)."


@pytest.mark.asyncio
async def test_new_command_does_not_require_backend_to_store_thread_before_ack() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
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
    assert messages[0].text == "[System] Started thread (id: thr_remote_new)."


@pytest.mark.asyncio
async def test_threads_command_prefers_native_thread_listing() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)
    backend = FakeBackend()
    from imcodex.bridge.thread_directory import NativeThreadSnapshot

    backend.list_threads_result = [
        NativeThreadSnapshot(
            thread_id="thr_native_1",
            cwd=r"D:\work\alpha",
            preview="Inspect tests",
            status="inProgress",
            name="Investigate alpha",
            path=r"D:\work\alpha",
        ),
        NativeThreadSnapshot(
            thread_id="thr_native_2",
            cwd=r"D:\work\alpha",
            preview="Ready",
            status="completed",
            name="Fix beta",
            path=r"D:\work\alpha",
        ),
    ]
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
            text="/threads",
        )
    )

    assert backend.list_threads_calls == [("qq", "conv-1", False)]
    assert messages[0].message_type == "command_result"
    assert messages[0].text.startswith(f"[System] Threads in CWD {cwd}:")
    assert "Investigate alpha" in messages[0].text
    assert "Fix beta" in messages[0].text
    assert "status: in progress" in messages[0].text
    assert "status: completed" in messages[0].text


@pytest.mark.asyncio
async def test_thread_read_command_prefers_native_thread_read() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    binding = store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
    binding.active_thread_id = "thr_native"
    backend = FakeBackend()
    from imcodex.bridge.thread_directory import NativeThreadSnapshot

    backend.read_thread_result = NativeThreadSnapshot(
        thread_id="thr_native",
        cwd=r"D:\work\alpha",
        preview="Inspect tests",
        status="awaitingUserInput",
        name="Investigate alpha",
        path=r"D:\work\alpha",
    )
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
            text="/thread read",
        )
    )

    assert backend.read_thread_calls == [("qq", "conv-1", "thr_native")]
    assert messages[0].message_type == "command_result"
    assert "Thread: Investigate alpha" in messages[0].text
    assert "Path: D:\\work\\alpha" in messages[0].text
    assert "Status: awaiting user input" in messages[0].text


@pytest.mark.asyncio
async def test_threads_command_falls_back_to_local_state_when_native_query_fails() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_local", cwd=r"D:\work\alpha", preview="Local preview")
    store.set_selected_cwd("qq", "conv-1", thread.cwd)
    backend = FakeBackend()
    backend.fail_list_threads = True
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
            text="/threads",
        )
    )

    assert backend.list_threads_calls == [("qq", "conv-1", False)]
    assert messages[0].message_type == "status"
    assert "could not be refreshed from Codex" in messages[0].text
    assert "/status" in messages[0].text
    assert "/thread read" in messages[0].text


@pytest.mark.asyncio
async def test_threads_command_without_selected_cwd_preserves_missing_project_guard() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    backend = FakeBackend()
    from imcodex.bridge.thread_directory import NativeThreadSnapshot

    backend.list_threads_result = [
        NativeThreadSnapshot(
            thread_id="thr_native",
            cwd=r"D:\work\alpha",
            preview="Native preview",
            status="idle",
            name="Native thread",
            path=r"D:\work\alpha",
        )
    ]
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
            text="/threads",
        )
    )

    assert backend.list_threads_calls == []
    assert messages[0].message_type == "command_result"
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_thread_read_command_surfaces_transient_status_when_native_query_fails() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_local", cwd=r"D:\work\alpha", preview="Local preview")
    binding = store.set_selected_cwd("qq", "conv-1", thread.cwd)
    binding.active_thread_id = "thr_local"
    backend = FakeBackend()
    backend.fail_read_thread = True
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
            text="/thread read",
        )
    )

    assert backend.read_thread_calls == [("qq", "conv-1", "thr_local")]
    assert messages[0].message_type == "status"
    assert "could not be queried from Codex right now" in messages[0].text
    assert "Try again in a moment." in messages[0].text


@pytest.mark.asyncio
async def test_threads_all_command_requests_cross_workspace_native_listing() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
    backend = FakeBackend()
    from imcodex.bridge.thread_directory import NativeThreadSnapshot

    backend.list_threads_result = [
        NativeThreadSnapshot(
            thread_id="thr_alpha",
            cwd=r"D:\work\alpha",
            preview="Alpha",
            status="idle",
            name="Alpha thread",
            path=r"D:\work\alpha",
        ),
        NativeThreadSnapshot(
            thread_id="thr_beta",
            cwd=r"D:\work\beta",
            preview="Beta",
            status="idle",
            name="Beta thread",
            path=r"D:\work\beta",
        ),
    ]
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
            text="/threads --all",
        )
    )

    assert backend.list_threads_calls == [("qq", "conv-1", True)]
    assert messages[0].text.startswith("[System] Threads across CWDs:")
    assert "Alpha thread" in messages[0].text
    assert "Beta thread" in messages[0].text
    assert "cwd: D:\\work\\beta" in messages[0].text
    assert "status: idle" in messages[0].text


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
async def test_new_command_does_not_persist_pending_label_across_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class CreatingBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=cwd, preview="")
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

    assert messages[0].text == "[System] Accepted. Processing started."
    assert reloaded_store.get_binding("qq", "conv-1").active_thread_id == "thr_remote_new"
    assert "thr_remote_new" not in [thread.thread_id for thread in reloaded_store.list_threads()]


@pytest.mark.asyncio
async def test_new_command_recovery_clears_stale_pending_label_from_abandoned_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class RecoveringBackend(FakeBackend):
        async def create_new_thread(self, channel_id: str, conversation_id: str) -> str:
            self.created_threads.append((channel_id, conversation_id))
            store.record_thread("thr_remote_new", cwd=cwd, preview="")
            store.set_active_thread(channel_id, conversation_id, "thr_remote_new")
            return "thr_remote_new"

        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            if text.startswith("please inspect"):
                store.record_thread("thr_recovered", cwd=cwd, preview="")
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

    assert store.thread_label("thr_recovered") == "Untitled thread"

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

    assert messages[0].text == "[System] Accepted. Processing started."
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
    assert "cwd" in messages[0].text.lower()


@pytest.mark.asyncio
async def test_plain_text_surfaces_recoverable_message_when_bound_thread_cannot_resume() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_stale", cwd=r"D:\work\alpha", preview="Stale thread")
    store.set_active_thread("qq", "conv-1", thread.thread_id)

    class StaleBackend(FakeBackend):
        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            raise StaleThreadBindingError("thr_stale")

    backend = StaleBackend()
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
            text="please continue the investigation",
        )
    )

    assert backend.started_turns == [("qq", "conv-1", "please continue the investigation")]
    assert messages[0].message_type == "status"
    assert "could not be resumed" in messages[0].text
    assert "/recover" in messages[0].text
    assert "/new" in messages[0].text


@pytest.mark.asyncio
async def test_recover_command_clears_stale_binding_for_next_prompt() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_stale", cwd=r"D:\work\alpha", preview="Stale thread")
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
            text="/recover",
        )
    )

    binding = store.get_binding("qq", "conv-1")
    assert messages[0].message_type == "status"
    assert "[System] Cleared stale thread binding thr_stale." in messages[0].text
    assert binding.active_thread_id is None
    assert binding.selected_cwd == r"D:\work\alpha"


@pytest.mark.asyncio
async def test_server_approval_request_is_still_shown_in_autonomous_mode() -> None:
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
async def test_plain_text_does_not_invent_thread_labels_from_first_user_message() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class RecordingBackend(FakeBackend):
        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            store.record_thread("thr_new", cwd=cwd, preview="")
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

    assert messages[0].text == "[System] Accepted. Processing started."
    assert store.thread_label("thr_new") == "Untitled thread"


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

    assert messages[0].text == "[System] Accepted. Processing started."
    assert store.thread_label("thr_existing") == "Untitled thread"


def test_bridge_service_autowires_registry_and_turn_state_into_plain_projector() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    backend = FakeBackend()
    projector = MessageProjector()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=projector,
    )

    assert service.projector.request_registry is service.request_registry
    assert service.projector.turn_state is service.turn_state
    assert service.projector.session_registry is service.session_registry
    assert service.projector.visibility.session_registry is service.session_registry


def test_bridge_service_rebinds_preconfigured_projector_and_visibility_to_service_registry() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    backend = FakeBackend()
    stale_registry = SessionRegistry(store)
    stale_request_registry = RequestRegistry(store)
    stale_turn_state = TurnStateMachine()
    projector = MessageProjector(
        request_registry=stale_request_registry,
        turn_state=stale_turn_state,
        session_registry=stale_registry,
        visibility=VisibilityClassifier(session_registry=stale_registry),
    )

    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=projector,
    )

    assert service.request_registry is stale_request_registry
    assert service.turn_state is stale_turn_state
    assert service.session_registry is stale_registry
    assert service.projector.request_registry is service.request_registry
    assert service.projector.turn_state is service.turn_state
    assert service.projector.session_registry is service.session_registry
    assert service.projector.visibility.session_registry is service.session_registry


@pytest.mark.asyncio
async def test_plain_text_emits_operational_logs(caplog) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    cwd = r"D:\work\alpha"
    store.set_selected_cwd("qq", "conv-1", cwd)

    class RecordingBackend(FakeBackend):
        async def start_turn(self, channel_id: str, conversation_id: str, text: str) -> str:
            self.started_turns.append((channel_id, conversation_id, text))
            store.record_thread("thr_new", cwd=cwd, preview="repo help")
            store.set_active_turn(
                channel_id,
                conversation_id,
                thread_id="thr_new",
                turn_id="turn_1",
                status="inProgress",
            )
            return "turn_1"

    backend = RecordingBackend()
    service = BridgeService(
        store=store,
        backend=backend,
        command_router=CommandRouter(store),
        projector=MessageProjector(),
    )

    caplog.set_level(logging.INFO)
    await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="inspect the repo",
        )
    )

    messages = [record.message for record in caplog.records]
    assert any("Inbound message received" in message for message in messages)
    assert any("Accepted inbound message" in message for message in messages)

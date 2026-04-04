from __future__ import annotations

import asyncio

import pytest

from imcodex.appserver_client import AppServerError
from imcodex.backend import CodexBackend
from imcodex.store import ConversationStore


class FakeClient:
    def __init__(self) -> None:
        self.thread_starts: list[dict] = []
        self.turn_starts: list[dict] = []
        self.turn_steers: list[dict] = []
        self.turn_interrupts: list[dict] = []
        self.replies: list[tuple[str, dict]] = []
        self.fail_thread_ids: set[str] = set()
        self.fail_steer_messages: list[str] = []
        self.fail_interrupt = False

    async def start_thread(self, **params):
        self.thread_starts.append(params)
        return {"thread": {"id": "thr_new", "preview": "", "status": {"type": "idle"}}}

    async def start_turn(self, **params):
        self.turn_starts.append(params)
        if params["thread_id"] in self.fail_thread_ids:
            raise AppServerError("turn/start timed out after 15.0s")
        return {"turn": {"id": "turn_1", "status": "inProgress", "items": [], "error": None}}

    async def interrupt_turn(self, **params):
        self.turn_interrupts.append(params)
        if self.fail_interrupt:
            raise AppServerError("invalid request")
        return {}

    async def steer_turn(self, **params):
        self.turn_steers.append(params)
        if self.fail_steer_messages:
            raise AppServerError(self.fail_steer_messages.pop(0))
        return {"turnId": params["turn_id"]}

    async def reply_to_server_request(self, ticket_id: str, result: dict):
        self.replies.append((ticket_id, result))


def make_store() -> ConversationStore:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("seed", cwd="D:/repo/app", preview="seed")
    store.set_active_project("demo", "conv-1", thread.project_id)
    return store


@pytest.mark.asyncio
async def test_ensure_thread_creates_thread_when_none_bound() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.ensure_thread("demo", "conv-1")

    assert thread_id == "thr_new"
    assert store.get_binding("demo", "conv-1").active_thread_id == "thr_new"
    assert client.thread_starts == [
        {"cwd": "D:/repo/app", "approval_policy": None, "sandbox": None, "model": None, "personality": "friendly", "service_name": "imcodex-test"}
    ]


@pytest.mark.asyncio
async def test_start_turn_tracks_active_turn() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    turn_id = await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert turn_id == "turn_1"
    assert store.get_binding("demo", "conv-1").active_turn_id == "turn_1"
    assert client.turn_starts == [
        {"thread_id": "thr_new", "text": "Please inspect the repo", "cwd": None, "model": None, "approval_policy": None, "sandbox_policy": None, "effort": None, "summary": "concise"}
    ]


@pytest.mark.asyncio
async def test_interrupt_turn_uses_bound_thread_and_turn() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"

    await backend.interrupt_active_turn("demo", "conv-1")

    assert client.turn_interrupts == [
        {"thread_id": "thr_existing", "turn_id": "turn_existing"}
    ]
    assert binding.active_turn_id is None


@pytest.mark.asyncio
async def test_start_turn_retries_with_new_thread_when_bound_thread_is_stale() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_stale"
    binding.known_thread_ids.append("thr_stale")
    client.fail_thread_ids.add("thr_stale")

    turn_id = await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert turn_id == "turn_1"
    assert client.thread_starts == [
        {"cwd": "D:/repo/app", "approval_policy": None, "sandbox": None, "model": None, "personality": "friendly", "service_name": "imcodex-test"}
    ]
    assert client.turn_starts == [
        {"thread_id": "thr_stale", "text": "Please inspect the repo", "cwd": None, "model": None, "approval_policy": None, "sandbox_policy": None, "effort": None, "summary": "concise"},
        {"thread_id": "thr_new", "text": "Please inspect the repo", "cwd": None, "model": None, "approval_policy": None, "sandbox_policy": None, "effort": None, "summary": "concise"},
    ]
    assert binding.active_thread_id == "thr_new"
    assert binding.active_turn_id == "turn_1"


@pytest.mark.asyncio
async def test_start_turn_steers_active_in_progress_turn() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"

    turn_id = await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")

    assert turn_id == "turn_existing"
    assert client.turn_steers == [
        {
            "thread_id": "thr_existing",
            "turn_id": "turn_existing",
            "text": "Actually focus on failing tests first",
        }
    ]
    assert client.turn_starts == []
    assert binding.active_turn_id == "turn_existing"
    assert binding.active_turn_status == "inProgress"


@pytest.mark.asyncio
async def test_start_turn_falls_back_to_interrupt_and_new_turn_when_steer_fails() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    client.fail_steer_messages = ["invalid request"]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"

    turn_id = await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")

    assert turn_id == "turn_1"
    assert client.turn_interrupts == [
        {"thread_id": "thr_existing", "turn_id": "turn_existing"}
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_existing",
            "text": "Actually focus on failing tests first",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "effort": None,
            "summary": "concise",
        }
    ]
    assert binding.active_turn_id == "turn_1"
    assert binding.active_turn_status == "inProgress"


@pytest.mark.asyncio
async def test_start_turn_does_not_interrupt_or_restart_on_transport_steer_failure() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    client.fail_steer_messages = ["turn/steer timed out after 15.0s"]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"

    with pytest.raises(AppServerError, match="timed out"):
        await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")

    assert client.turn_interrupts == []
    assert client.turn_starts == []
    assert binding.active_turn_id == "turn_existing"
    assert binding.active_turn_status == "inProgress"


@pytest.mark.asyncio
async def test_start_turn_recovers_when_steer_is_rejected_and_interrupt_also_fails() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    client.fail_steer_messages = ["invalid request"]
    client.fail_interrupt = True
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"

    turn_id = await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")

    assert turn_id == "turn_1"
    assert client.turn_interrupts == [
        {"thread_id": "thr_existing", "turn_id": "turn_existing"}
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_existing",
            "text": "Actually focus on failing tests first",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "effort": None,
            "summary": "concise",
        }
    ]
    assert binding.active_turn_id == "turn_1"


@pytest.mark.asyncio
async def test_start_turn_retries_steer_when_server_has_not_marked_turn_active_yet() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    client.fail_steer_messages = ["no active turn to steer"]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"

    started = asyncio.get_running_loop().time()
    turn_id = await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")
    elapsed = asyncio.get_running_loop().time() - started

    assert turn_id == "turn_existing"
    assert len(client.turn_steers) == 2
    assert client.turn_interrupts == []
    assert client.turn_starts == []
    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_reply_to_server_request_uses_server_request_id_when_present() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="7",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        request_id="99",
        request_method="item/commandExecution/requestApproval",
    )

    await backend.reply_to_server_request("7", {"decision": "accept"})

    assert client.replies == [("99", {"decision": "accept"})]

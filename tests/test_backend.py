from __future__ import annotations

import pytest

from imcodex.backend import CodexBackend
from imcodex.store import ConversationStore


class FakeClient:
    def __init__(self) -> None:
        self.thread_starts: list[dict] = []
        self.turn_starts: list[dict] = []
        self.turn_interrupts: list[dict] = []

    async def start_thread(self, **params):
        self.thread_starts.append(params)
        return {"thread": {"id": "thr_new", "preview": "", "status": {"type": "idle"}}}

    async def start_turn(self, **params):
        self.turn_starts.append(params)
        return {"turn": {"id": "turn_1", "status": "inProgress", "items": [], "error": None}}

    async def interrupt_turn(self, **params):
        self.turn_interrupts.append(params)
        return {}

    async def reply_to_server_request(self, ticket_id: str, result: dict):
        raise AssertionError("not used in this test")


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

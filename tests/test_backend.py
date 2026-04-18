from __future__ import annotations

import pytest

from imcodex.appserver import AppServerError, CodexBackend, ThreadSelectionError
from imcodex.store import ConversationStore


class FakeClient:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []

    async def list_threads(self, **params):
        self.list_calls.append(params)
        return {
            "data": [
                {
                    "id": "thr_same_cwd",
                    "cwd": r"D:\desktop\imcodex",
                    "preview": "same cwd",
                    "status": {"type": "notLoaded"},
                    "source": "vscode",
                },
                {
                    "id": "thr_other",
                    "cwd": r"D:\elsewhere",
                    "preview": "other cwd",
                    "status": {"type": "notLoaded"},
                    "source": "cli",
                },
            ],
            "nextCursor": None,
        }

    async def read_thread(self, thread_id: str):
        raise AssertionError(f"read_thread should not be called for listed thread {thread_id}")


class NamedClient(FakeClient):
    def __init__(self, items: list[dict]) -> None:
        super().__init__()
        self.items = items

    async def list_threads(self, **params):
        self.list_calls.append(params)
        return {"data": list(self.items), "nextCursor": None}


class NewThreadClient:
    def __init__(self) -> None:
        self.resume_calls: list[dict] = []
        self.start_turn_calls: list[dict] = []
        self.start_thread_calls: list[dict] = []

    async def start_thread(self, **params):
        self.start_thread_calls.append(params)
        return {
            "thread": {
                "id": "thr_new",
                "cwd": r"D:\desktop\imcodex",
                "preview": "New thread",
                "status": "idle",
            }
        }

    async def resume_thread(self, **params):
        self.resume_calls.append(params)
        raise AssertionError("resume_thread should not be called for a freshly started thread")

    async def start_turn(self, thread_id: str, text: str, **kwargs):
        self.start_turn_calls.append({"thread_id": thread_id, "text": text, **kwargs})
        return {"turn": {"id": "turn_1", "status": "inProgress"}}


class ResumeFallbackClient:
    def __init__(self) -> None:
        self.resume_calls: list[dict] = []
        self.start_turn_calls: list[dict] = []
        self._start_attempts = 0

    async def start_turn(self, thread_id: str, text: str, **kwargs):
        self._start_attempts += 1
        self.start_turn_calls.append({"thread_id": thread_id, "text": text, **kwargs})
        if self._start_attempts == 1:
            raise AppServerError(f"no rollout found for thread id {thread_id}")
        return {"turn": {"id": "turn_2", "status": "inProgress"}}

    async def resume_thread(self, **params):
        self.resume_calls.append(params)
        return {
            "thread": {
                "id": "thr_old",
                "cwd": r"D:\desktop\imcodex",
                "preview": "Old thread",
                "status": "idle",
            }
        }


class InterruptStaleClient:
    def __init__(self, message: str) -> None:
        self.message = message
        self.interrupt_calls: list[dict] = []

    async def interrupt_turn(self, thread_id: str, turn_id: str):
        self.interrupt_calls.append({"thread_id": thread_id, "turn_id": turn_id})
        raise AppServerError(self.message)


@pytest.mark.asyncio
async def test_list_threads_accepts_data_key_and_prioritizes_preferred_cwd() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    threads = await backend.list_threads("qq", "conv-1")

    assert [thread.thread_id for thread in threads] == ["thr_same_cwd", "thr_other"]
    assert client.list_calls == [{"sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "appServer"]}]


@pytest.mark.asyncio
async def test_resolve_thread_selector_matches_name_prefix() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = NamedClient(
        [
            {
                "id": "thr_named",
                "cwd": r"D:\desktop\imcodex",
                "preview": "same cwd",
                "name": "Repo polish",
                "status": {"type": "notLoaded"},
                "source": "vscode",
            }
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    snapshot = await backend.resolve_thread_selector("qq", "conv-1", "repo pol")

    assert snapshot.thread_id == "thr_named"


@pytest.mark.asyncio
async def test_resolve_thread_selector_rejects_ambiguous_label_prefix() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = NamedClient(
        [
            {
                "id": "thr_1",
                "cwd": r"D:\desktop\imcodex",
                "preview": "same cwd",
                "name": "Bug bash",
                "status": {"type": "notLoaded"},
                "source": "vscode",
            },
            {
                "id": "thr_2",
                "cwd": r"D:\desktop\other",
                "preview": "other cwd",
                "name": "Bug backlog",
                "status": {"type": "notLoaded"},
                "source": "cli",
            },
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(ThreadSelectionError, match="matches multiple threads"):
        await backend.resolve_thread_selector("qq", "conv-1", "bug")


@pytest.mark.asyncio
async def test_submit_text_reuses_fresh_thread_without_resume() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = NewThreadClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.create_new_thread("qq", "conv-1")
    submission = await backend.submit_text("qq", "conv-1", "hi")

    assert thread_id == "thr_new"
    assert submission.thread_id == "thr_new"
    assert submission.turn_id == "turn_1"
    assert client.resume_calls == []
    assert client.start_turn_calls == [{"thread_id": "thr_new", "text": "hi", "summary": "concise"}]


@pytest.mark.asyncio
async def test_submit_text_resumes_loaded_thread_after_missing_rollout_error() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    store.bind_thread("qq", "conv-1", "thr_old")
    client = ResumeFallbackClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    submission = await backend.submit_text("qq", "conv-1", "continue")

    assert submission.thread_id == "thr_old"
    assert submission.turn_id == "turn_2"
    assert client.resume_calls == [
        {
            "thread_id": "thr_old",
            "service_name": "imcodex-test",
            "personality": "friendly",
        }
    ]
    assert client.start_turn_calls == [
        {"thread_id": "thr_old", "text": "continue", "summary": "concise"},
        {"thread_id": "thr_old", "text": "continue", "summary": "concise"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["unknown thread", "no rollout found for thread id thr_old"])
async def test_interrupt_turn_treats_stale_thread_errors_as_local_cleanup(message: str) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_old")
    store.note_active_turn("thr_old", "turn_1", "inProgress")
    client = InterruptStaleClient(message)
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    interrupted = await backend.interrupt_turn("thr_old", "turn_1")

    assert interrupted is False
    assert store.get_active_turn("thr_old") is None
    assert client.interrupt_calls == [{"thread_id": "thr_old", "turn_id": "turn_1"}]

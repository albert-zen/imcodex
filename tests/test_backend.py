from __future__ import annotations

import pytest

from imcodex.appserver import AppServerError, CodexBackend, ThreadSelectionError
from imcodex.appserver.backend import (
    ACTIVE_THREAD_STATUSES,
    PERMISSION_MODE_PROFILE_IDS,
    ThreadListResult,
)
from imcodex.store import ConversationStore


def test_backend_module_keeps_split_compatibility_exports() -> None:
    assert "running" in ACTIVE_THREAD_STATUSES
    assert PERMISSION_MODE_PROFILE_IDS["read-only"] == ":read-only"
    assert ThreadListResult(threads=[]).threads == []


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
        self.last_connection_mode = "spawned-stdio"

    async def interrupt_turn(self, thread_id: str, turn_id: str):
        self.interrupt_calls.append({"thread_id": thread_id, "turn_id": turn_id})
        raise AppServerError(self.message)


class RehydrateClient:
    def __init__(self, *, status: str = "idle") -> None:
        self.resume_calls: list[dict] = []
        self.status = status

    async def resume_thread(self, **params):
        self.resume_calls.append(params)
        return {
            "thread": {
                "id": params["thread_id"],
                "cwd": r"D:\desktop\imcodex",
                "preview": "Recovered thread",
                "status": self.status,
            }
        }


class AttachClient:
    def __init__(self) -> None:
        self.resume_calls: list[dict] = []

    async def resume_thread(self, **params):
        self.resume_calls.append(params)
        return {
            "thread": {
                "id": params["thread_id"],
                "cwd": r"D:\desktop\attached",
                "preview": "Attached thread",
                "status": "idle",
            }
        }


class ThreadOpsClient:
    def __init__(self) -> None:
        self.history_calls: list[dict] = []
        self.fork_calls: list[str] = []
        self.rename_calls: list[dict] = []
        self.compact_calls: list[str] = []

    async def list_thread_turns(self, thread_id: str, **params):
        self.history_calls.append({"thread_id": thread_id, **params})
        return {"turns": []}

    async def fork_thread(self, thread_id: str):
        self.fork_calls.append(thread_id)
        return {
            "thread": {
                "id": "thr_forked",
                "cwd": r"D:\desktop\attached",
                "preview": "Forked thread",
                "status": "idle",
            }
        }

    async def set_thread_name(self, thread_id: str, name: str):
        self.rename_calls.append({"thread_id": thread_id, "name": name})
        return {"ok": True}

    async def compact_thread(self, thread_id: str):
        self.compact_calls.append(thread_id)
        return {"ok": True}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("dedicated-ws", True),
        ("shared-ws", True),
        ("spawned-stdio", False),
        ("disconnected", False),
    ],
)
def test_prefers_native_recovery_for_websocket_modes(mode: str, expected: bool) -> None:
    client = type("Client", (), {"connection_mode": mode, "last_connection_mode": "disconnected"})()
    backend = CodexBackend(client=client, store=ConversationStore(clock=lambda: 1.0), service_name="imcodex")

    assert backend.prefers_native_recovery() is expected


def test_prefers_native_recovery_falls_back_to_last_connection_mode() -> None:
    client = type("Client", (), {"connection_mode": "disconnected", "last_connection_mode": "dedicated-ws"})()
    backend = CodexBackend(client=client, store=ConversationStore(clock=lambda: 1.0), service_name="imcodex")

    assert backend.prefers_native_recovery() is True


@pytest.mark.asyncio
async def test_list_threads_accepts_data_key_and_prioritizes_preferred_cwd() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    threads = await backend.list_threads("qq", "conv-1")

    assert [thread.thread_id for thread in threads] == ["thr_same_cwd", "thr_other"]
    assert client.list_calls == [{"sortKey": "updated_at"}]


@pytest.mark.asyncio
async def test_list_threads_prioritizes_windows_extended_length_cwd_match() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = NamedClient(
        [
            {
                "id": "thr_projectless",
                "cwd": r"\\?\D:\desktop\imcodex",
                "preview": "projectless cwd form",
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
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    threads = await backend.list_threads("qq", "conv-1")

    assert [thread.thread_id for thread in threads] == ["thr_projectless", "thr_other"]


@pytest.mark.asyncio
async def test_query_threads_sends_native_search_and_limit() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = NamedClient(
        [
            {
                "id": "thr_named",
                "cwd": r"D:\desktop\imcodex",
                "preview": "native match",
                "status": {"type": "notLoaded"},
                "source": "vscode",
            }
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_threads("qq", "conv-1", search_term="native match", limit=6)

    assert [thread.thread_id for thread in result.threads] == ["thr_named"]
    assert client.list_calls == [{"sortKey": "updated_at", "searchTerm": "native match", "limit": 6}]


@pytest.mark.asyncio
async def test_query_threads_does_not_inject_bound_thread_into_cursored_native_page() -> None:
    class CursoredClient(NamedClient):
        async def list_threads(self, **params):
            self.list_calls.append(params)
            return {"data": list(self.items), "nextCursor": "cursor-2"}

        async def read_thread(self, thread_id: str):
            raise AssertionError(f"read_thread should not be called for cursored page {thread_id}")

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_bound")
    client = CursoredClient(
        [
            {"id": f"thr_{index}", "cwd": rf"D:\work\{index}", "preview": f"Thread {index}", "status": "idle"}
            for index in range(1, 6)
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_threads("qq", "conv-1", limit=5)

    assert [thread.thread_id for thread in result.threads] == ["thr_1", "thr_2", "thr_3", "thr_4", "thr_5"]
    assert result.next_cursor == "cursor-2"
    assert client.list_calls == [{"sortKey": "updated_at", "limit": 5}]


@pytest.mark.asyncio
async def test_query_threads_does_not_inject_bound_thread_into_full_terminal_native_page() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_bound")
    client = NamedClient(
        [
            {"id": f"thr_{index}", "cwd": rf"D:\work\{index}", "preview": f"Thread {index}", "status": "idle"}
            for index in range(1, 6)
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_threads("qq", "conv-1", limit=5)

    assert [thread.thread_id for thread in result.threads] == ["thr_1", "thr_2", "thr_3", "thr_4", "thr_5"]
    assert result.next_cursor is None
    assert client.list_calls == [{"sortKey": "updated_at", "limit": 5}]


@pytest.mark.asyncio
async def test_attach_thread_resumes_selected_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = AttachClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.attach_thread("qq", "conv-1", "thr_attached")

    assert thread_id == "thr_attached"
    assert client.resume_calls == [
        {
            "thread_id": "thr_attached",
            "service_name": "imcodex-test",
            "personality": "friendly",
        }
    ]
    assert store.get_binding("qq", "conv-1").thread_id == "thr_attached"
    assert store.get_binding("qq", "conv-1").bootstrap_cwd == r"D:\desktop\attached"


@pytest.mark.asyncio
async def test_thread_operations_use_active_native_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\attached")
    client = ThreadOpsClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    history = await backend.read_thread_history("qq", "conv-1", limit=3)
    forked = await backend.fork_thread("qq", "conv-1")
    await backend.rename_thread("qq", "conv-1", "Renamed thread")
    await backend.compact_thread("qq", "conv-1")

    assert history == {"turns": []}
    assert forked.thread_id == "thr_forked"
    assert store.get_binding("qq", "conv-1").thread_id == "thr_forked"
    assert client.history_calls == [{"thread_id": "thr_1", "limit": 3}]
    assert client.fork_calls == ["thr_1"]
    assert client.rename_calls == [{"thread_id": "thr_forked", "name": "Renamed thread"}]
    assert client.compact_calls == ["thr_forked"]


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


@pytest.mark.asyncio
async def test_rehydrate_bound_threads_resumes_all_known_bindings() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.bind_thread_with_cwd("debug", "conv-2", "thr_2", r"D:\desktop\imcodex")
    client = RehydrateClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.rehydrate_bound_threads()

    assert client.resume_calls == [
        {"thread_id": "thr_1", "service_name": "imcodex-test", "personality": "friendly"},
        {"thread_id": "thr_2", "service_name": "imcodex-test", "personality": "friendly"},
    ]
    assert store.get_thread_snapshot("thr_1").preview == "Recovered thread"
    assert store.get_thread_snapshot("thr_2").preview == "Recovered thread"


@pytest.mark.asyncio
async def test_rehydrate_bound_threads_clears_active_turn_when_native_thread_is_idle() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = RehydrateClient(status="idle")
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["inProgress", "in_progress", "running", "working"])
async def test_rehydrate_bound_threads_keeps_active_turn_when_native_thread_is_active(status: str) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = RehydrateClient(status=status)
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") == ("turn_1", "inProgress")

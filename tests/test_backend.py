from __future__ import annotations

import pytest

from imcodex.appserver import AppServerError, CodexBackend, ThreadSelectionError
from imcodex.appserver.backend import (
    ACTIVE_THREAD_STATUSES,
    PERMISSION_MODE_PROFILE_IDS,
    ThreadListResult,
)
from imcodex.models import InboundAttachment
from imcodex.store import ConversationStore


def test_backend_module_keeps_split_compatibility_exports() -> None:
    assert "running" in ACTIVE_THREAD_STATUSES
    assert PERMISSION_MODE_PROFILE_IDS["read-only"] == ":read-only"
    assert ThreadListResult(threads=[]).threads == []


@pytest.mark.parametrize("connection_mode", [None, ""])
def test_connection_facts_fallback_treats_empty_modes_as_disconnected(
    connection_mode,
) -> None:
    client = type(
        "ConnectionModeOnlyClient",
        (),
        {
            "connection_mode": connection_mode,
            "initialized": True,
            "connection_epoch": 0,
        },
    )()
    backend = CodexBackend(
        client=client,
        store=ConversationStore(clock=lambda: 1.0),
        service_name="imcodex-test",
    )

    facts = backend.app_server_connection_facts()

    assert facts["connected"] is False
    assert facts["ready"] is False
    assert facts["status"] == "disconnected"
    assert facts["ownership"] == "unknown"


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
        return {
            "thread": {
                "id": "thr_new",
                "cwd": r"D:\desktop\imcodex",
                "preview": "New thread",
                "status": "idle",
            }
        }

    async def start_turn(self, thread_id: str, *, input_items: list[dict], **kwargs):
        self.start_turn_calls.append({"thread_id": thread_id, "input_items": input_items, **kwargs})
        return {"turn": {"id": "turn_1", "status": "inProgress"}}


class ResumeFallbackClient:
    def __init__(self) -> None:
        self.resume_calls: list[dict] = []
        self.start_turn_calls: list[dict] = []
        self._start_attempts = 0

    async def start_turn(self, thread_id: str, *, input_items: list[dict], **kwargs):
        self._start_attempts += 1
        self.start_turn_calls.append({"thread_id": thread_id, "input_items": input_items, **kwargs})
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


class MultimodalClient:
    def __init__(self, *, stale_steer: bool = False) -> None:
        self.stale_steer = stale_steer
        self.start_turn_calls: list[dict] = []
        self.steer_turn_calls: list[dict] = []

    def supports_local_image_paths(self) -> bool:
        return True

    async def start_turn(self, thread_id: str, *, input_items: list[dict], **kwargs):
        self.start_turn_calls.append({"thread_id": thread_id, "input_items": input_items, **kwargs})
        return {"turn": {"id": "turn_2", "status": "inProgress"}}

    async def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        input_items: list[dict],
    ) -> dict:
        self.steer_turn_calls.append(
            {"thread_id": thread_id, "turn_id": turn_id, "input_items": input_items}
        )
        if self.stale_steer:
            raise AppServerError("no active turn")
        return {"turnId": turn_id}


class InterruptStaleClient:
    def __init__(self, message: str) -> None:
        self.message = message
        self.interrupt_calls: list[dict] = []
        self.last_connection_mode = "spawned-stdio"

    async def interrupt_turn(self, thread_id: str, turn_id: str):
        self.interrupt_calls.append({"thread_id": thread_id, "turn_id": turn_id})
        raise AppServerError(self.message)


class RehydrateClient:
    def __init__(self, *, status: object = "idle", turns: list[dict] | None = None) -> None:
        self.resume_calls: list[dict] = []
        self.status = status
        self.turns = turns

    async def resume_thread(self, **params):
        self.resume_calls.append(params)
        thread = {
            "id": params["thread_id"],
            "cwd": r"D:\desktop\imcodex",
            "preview": "Recovered thread",
            "status": self.status,
        }
        if self.turns is not None:
            thread["turns"] = list(self.turns)
        return {"thread": thread}


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
        ("external", True),
        ("dedicated-ws", True),
        ("shared-ws", True),
        ("spawned-stdio", False),
        ("disconnected", False),
    ],
)
def test_prefers_native_recovery_for_websocket_modes(mode: str, expected: bool) -> None:
    client = type("Client", (), {"connection_mode": mode, "last_connection_mode": "disconnected"})()
    backend = CodexBackend(
        client=client,
        store=ConversationStore(clock=lambda: 1.0),
        service_name="imcodex",
    )

    assert backend.prefers_native_recovery() is expected


def test_prefers_native_recovery_falls_back_to_last_connection_mode() -> None:
    client = type(
        "Client",
        (),
        {"connection_mode": "disconnected", "last_connection_mode": "dedicated-ws"},
    )()
    backend = CodexBackend(
        client=client,
        store=ConversationStore(clock=lambda: 1.0),
        service_name="imcodex",
    )

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
async def test_query_all_threads_exhausts_native_cursors_in_desktop_sized_batches() -> None:
    class PagedClient(FakeClient):
        async def list_threads(self, **params):
            self.list_calls.append(params)
            if params.get("cursor") == "cursor-2":
                return {
                    "data": [
                        {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "Alpha refreshed",
                            "status": "idle",
                        },
                        {
                            "id": "thr_2",
                            "cwd": r"D:\work\beta",
                            "preview": "Beta",
                            "status": "idle",
                        }
                    ],
                    "nextCursor": None,
                }
            return {
                "data": [
                    {
                        "id": "thr_1",
                        "cwd": r"D:\work\alpha",
                        "preview": "Alpha",
                        "status": "idle",
                    }
                ],
                "nextCursor": "cursor-2",
            }

    store = ConversationStore(clock=lambda: 1.0)
    client = PagedClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_all_threads("qq", "conv-1", search_term="release")

    assert [thread.thread_id for thread in result.threads] == ["thr_1", "thr_2"]
    assert result.threads[0].preview == "Alpha refreshed"
    assert result.next_cursor is None
    assert client.list_calls == [
        {
            "sortKey": "updated_at",
            "limit": 100,
            "searchTerm": "release",
        },
        {
            "sortKey": "updated_at",
            "limit": 100,
            "searchTerm": "release",
            "cursor": "cursor-2",
        },
    ]


@pytest.mark.asyncio
async def test_query_all_threads_rejects_repeated_native_cursor() -> None:
    class RepeatingCursorClient(FakeClient):
        async def list_threads(self, **params):
            self.list_calls.append(params)
            return {"data": [], "nextCursor": "same-cursor"}

    backend = CodexBackend(
        client=RepeatingCursorClient(),
        store=ConversationStore(clock=lambda: 1.0),
        service_name="imcodex-test",
    )

    with pytest.raises(AppServerError, match="repeated pagination cursor"):
        await backend.query_all_threads("qq", "conv-1")


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
            {
                "id": f"thr_{index}",
                "cwd": rf"D:\work\{index}",
                "preview": f"Thread {index}",
                "status": "idle",
            }
            for index in range(1, 6)
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_threads("qq", "conv-1", limit=5)

    assert [thread.thread_id for thread in result.threads] == [
        "thr_1",
        "thr_2",
        "thr_3",
        "thr_4",
        "thr_5",
    ]
    assert result.next_cursor == "cursor-2"
    assert client.list_calls == [{"sortKey": "updated_at", "limit": 5}]


@pytest.mark.asyncio
async def test_query_threads_does_not_inject_bound_thread_into_full_terminal_native_page() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_bound")
    client = NamedClient(
        [
            {
                "id": f"thr_{index}",
                "cwd": rf"D:\work\{index}",
                "preview": f"Thread {index}",
                "status": "idle",
            }
            for index in range(1, 6)
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.query_threads("qq", "conv-1", limit=5)

    assert [thread.thread_id for thread in result.threads] == [
        "thr_1",
        "thr_2",
        "thr_3",
        "thr_4",
        "thr_5",
    ]
    assert result.next_cursor is None
    assert client.list_calls == [{"sortKey": "updated_at", "limit": 5}]


@pytest.mark.asyncio
async def test_attach_thread_preserves_existing_tools_without_resume_injection() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = AttachClient()
    backend = CodexBackend(
        client=client,
        store=store,
        service_name="imcodex-test",
        thread_dynamic_tools=[
            {
                "type": "function",
                "name": "list_threads",
                "description": "List threads",
                "inputSchema": {"type": "object"},
            }
        ],
    )

    thread_id = await backend.attach_thread("qq", "conv-1", "thr_attached")

    assert thread_id == "thr_attached"
    assert client.resume_calls == [
        {
            "thread_id": "thr_attached",
            "service_name": "imcodex-test",
        }
    ]
    assert store.get_binding("qq", "conv-1").thread_id == "thr_attached"
    assert store.get_binding("qq", "conv-1").bootstrap_cwd == r"D:\desktop\attached"


@pytest.mark.asyncio
async def test_attach_thread_reconciles_native_active_turn() -> None:
    class ActiveAttachClient:
        async def resume_thread(self, **_params):
            return {
                "thread": {
                    "id": "thr_running",
                    "cwd": r"D:\desktop\attached",
                    "preview": "Running thread",
                    "turns": [{"id": "turn_native", "status": "inProgress"}],
                }
            }

    store = ConversationStore(clock=lambda: 1.0)
    backend = CodexBackend(client=ActiveAttachClient(), store=store, service_name="imcodex-test")

    await backend.attach_thread("qq", "conv-1", "thr_running")

    assert store.get_active_turn("thr_running") == ("turn_native", "inProgress")


@pytest.mark.asyncio
async def test_attach_thread_refuses_inexact_native_handoff() -> None:
    class InexactAttachClient:
        async def resume_thread(self, **_params):
            return {
                "thread": {
                    "id": "thr_other",
                    "cwd": r"D:\desktop\attached",
                    "status": "idle",
                }
            }

    store = ConversationStore(clock=lambda: 1.0)
    backend = CodexBackend(client=InexactAttachClient(), store=store, service_name="test")

    with pytest.raises(AppServerError, match="inexact handoff"):
        await backend.attach_thread("qq", "conv-1", "thr_requested")

    assert store.get_binding("qq", "conv-1").thread_id is None


@pytest.mark.asyncio
async def test_attach_thread_refuses_unverifiable_active_handoff() -> None:
    class UnverifiableAttachClient:
        async def resume_thread(self, **params):
            return {
                "thread": {
                    "id": params["thread_id"],
                    "cwd": r"D:\desktop\attached",
                    "status": "active",
                    "turns": [],
                }
            }

    store = ConversationStore(clock=lambda: 1.0)
    backend = CodexBackend(client=UnverifiableAttachClient(), store=store, service_name="test")

    with pytest.raises(AppServerError, match="unverifiable handoff"):
        await backend.attach_thread("qq", "conv-1", "thr_running")

    assert store.get_binding("qq", "conv-1").thread_id is None


@pytest.mark.asyncio
async def test_attach_thread_allows_observing_nontransferable_native_interaction() -> None:
    class PendingInteractionClient:
        async def resume_thread(self, **params):
            return {
                "thread": {
                    "id": params["thread_id"],
                    "cwd": r"D:\desktop\attached",
                    "status": "active",
                    "canAcceptDirectInput": False,
                    "turns": [{"id": "turn_native", "status": "inProgress"}],
                }
            }

    store = ConversationStore(clock=lambda: 1.0)
    backend = CodexBackend(client=PendingInteractionClient(), store=store, service_name="test")

    attached = await backend.attach_thread("qq", "conv-1", "thr_running")

    assert attached == "thr_running"
    assert store.get_binding("qq", "conv-1").thread_id == "thr_running"
    assert store.get_active_turn("thr_running") == ("turn_native", "inProgress")


@pytest.mark.asyncio
async def test_observed_busy_thread_defers_input_acceptance_to_native_steer() -> None:
    class TemporarilyReadOnlyClient:
        _experimental_api_enabled = True

        def __init__(self) -> None:
            self.can_accept_direct_input = False
            self.steer_calls: list[dict] = []

        async def resume_thread(self, **params):
            return {
                "thread": {
                    "id": params["thread_id"],
                    "cwd": r"D:\desktop\attached",
                    "status": "active",
                    "canAcceptDirectInput": self.can_accept_direct_input,
                    "turns": [{"id": "turn_native", "status": "inProgress"}],
                }
            }

        async def steer_turn(self, thread_id: str, turn_id: str, **params):
            self.steer_calls.append(
                {"thread_id": thread_id, "turn_id": turn_id, **params}
            )
            return {"turnId": turn_id}

    store = ConversationStore(clock=lambda: 1.0)
    client = TemporarilyReadOnlyClient()
    backend = CodexBackend(client=client, store=store, service_name="test")

    await backend.attach_thread("qq", "conv-1", "thr_running")

    submission = await backend.submit_text("qq", "conv-1", "continue")

    assert submission.kind == "steer"
    assert client.steer_calls == [
        {
            "thread_id": "thr_running",
            "turn_id": "turn_native",
            "input_items": [{"type": "text", "text": "continue"}],
        }
    ]


@pytest.mark.asyncio
async def test_bound_thread_input_steers_without_experimental_direct_input_hint() -> None:
    class StableOnlyClient:
        _experimental_api_enabled = False

        def __init__(self) -> None:
            self.steer_calls: list[tuple[str, str]] = []

        async def resume_thread(self, **params):
            return {
                "thread": {
                    "id": params["thread_id"],
                    "cwd": r"D:\desktop\attached",
                    "status": "active",
                    "turns": [{"id": "turn_native", "status": "inProgress"}],
                }
            }

        async def steer_turn(self, thread_id: str, turn_id: str, **_params):
            self.steer_calls.append((thread_id, turn_id))
            return {"turnId": turn_id}

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_running")
    client = StableOnlyClient()
    backend = CodexBackend(client=client, store=store, service_name="test")

    submission = await backend.submit_text("qq", "conv-1", "continue")

    assert submission.kind == "steer"
    assert client.steer_calls == [("thr_running", "turn_native")]
    assert store.get_binding("qq", "conv-1").thread_id == "thr_running"


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

    assert history == {"turns": [], "page": 1, "hasOlder": False}
    assert forked.thread_id == "thr_forked"
    assert store.get_binding("qq", "conv-1").thread_id == "thr_forked"
    assert client.history_calls == [
        {
            "thread_id": "thr_1",
            "limit": 3,
            "items_view": "full",
            "sort_direction": "desc",
        }
    ]
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
async def test_submit_text_reconciles_fresh_thread_before_first_input() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = NewThreadClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.create_new_thread("qq", "conv-1")
    submission = await backend.submit_text("qq", "conv-1", "hi")

    assert thread_id == "thr_new"
    assert submission.thread_id == "thr_new"
    assert submission.turn_id == "turn_1"
    assert client.start_thread_calls == [
        {"cwd": r"D:\desktop\imcodex", "service_name": "imcodex-test"},
    ]
    assert client.resume_calls == [
        {"thread_id": "thr_new", "service_name": "imcodex-test"}
    ]
    assert client.start_turn_calls == [
        {
            "thread_id": "thr_new",
            "input_items": [{"type": "text", "text": "hi"}],
            "summary": "concise",
        }
    ]
@pytest.mark.asyncio
async def test_new_thread_receives_configured_dynamic_tools() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = NewThreadClient()
    tool_specs = [
        {
            "type": "function",
            "name": "list_threads",
            "description": "List threads",
            "inputSchema": {"type": "object"},
        }
    ]
    backend = CodexBackend(
        client=client,
        store=store,
        service_name="imcodex-test",
        thread_dynamic_tools=tool_specs,
    )

    await backend.create_new_thread("qq", "conv-1")
    tool_specs[0]["name"] = "mutated"

    assert client.start_thread_calls == [
        {
            "cwd": r"D:\desktop\imcodex",
            "service_name": "imcodex-test",
            "dynamicTools": [
                {
                    "type": "function",
                    "name": "list_threads",
                    "description": "List threads",
                    "inputSchema": {"type": "object"},
                }
            ],
        }
    ]


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
        },
        {
            "thread_id": "thr_old",
            "service_name": "imcodex-test",
        },
    ]
    assert client.start_turn_calls == [
        {
            "thread_id": "thr_old",
            "input_items": [{"type": "text", "text": "continue"}],
            "summary": "concise",
        },
        {
            "thread_id": "thr_old",
            "input_items": [{"type": "text", "text": "continue"}],
            "summary": "concise",
        },
    ]


@pytest.mark.asyncio
async def test_submit_text_steers_turn_discovered_during_resume_retry() -> None:
    class ActiveOnRetryClient:
        def __init__(self) -> None:
            self.resume_calls = 0
            self.start_turn_calls = 0
            self.steer_turn_calls: list[dict] = []

        async def resume_thread(self, **params):
            self.resume_calls += 1
            turns = []
            status = "idle"
            if self.resume_calls == 2:
                status = "active"
                turns = [{"id": "turn_native", "status": "inProgress"}]
            return {
                "thread": {
                    "id": params["thread_id"],
                    "cwd": r"D:\desktop\imcodex",
                    "status": status,
                    "turns": turns,
                }
            }

        async def start_turn(self, thread_id: str, **_params):
            self.start_turn_calls += 1
            raise AppServerError(f"no rollout found for thread id {thread_id}")

        async def steer_turn(self, thread_id: str, turn_id: str, **params):
            self.steer_turn_calls.append(
                {"thread_id": thread_id, "turn_id": turn_id, **params}
            )
            return {"turnId": turn_id}

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_old")
    client = ActiveOnRetryClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    submission = await backend.submit_text("qq", "conv-1", "continue")

    assert submission.kind == "steer"
    assert submission.turn_id == "turn_native"
    assert client.resume_calls == 2
    assert client.start_turn_calls == 1
    assert client.steer_turn_calls == [
        {
            "thread_id": "thr_old",
            "turn_id": "turn_native",
            "input_items": [{"type": "text", "text": "continue"}],
        }
    ]


@pytest.mark.asyncio
async def test_submit_input_starts_image_only_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    client = MultimodalClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    image = InboundAttachment(
        kind="image",
        content_type="image/png",
        local_path="/tmp/inbound.png",
        size_bytes=123,
    )

    submission = await backend.submit_input("qq", "conv-1", "", (image,))

    assert submission.kind == "start"
    assert client.start_turn_calls == [
        {
            "thread_id": "thr_1",
            "input_items": [
                {"type": "text", "text": "[Image]"},
                {"type": "localImage", "path": "/tmp/inbound.png"},
            ],
            "summary": "concise",
        }
    ]


@pytest.mark.asyncio
async def test_submit_input_binds_image_submission_to_capability_epoch() -> None:
    class EpochClient:
        def __init__(self) -> None:
            self.start_kwargs: dict[str, object] = {}

        def supports_local_image_paths(self) -> bool:
            return True

        def local_image_paths_epoch(self) -> int:
            return 7

        async def start_turn(self, **kwargs):
            self.start_kwargs = kwargs
            return {"turn": {"id": "turn_7", "status": "inProgress"}}

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    client = EpochClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    image = InboundAttachment("image", "image/png", "/tmp/inbound.png", 123)

    await backend.submit_input("qq", "conv-1", "inspect", (image,))

    assert client.start_kwargs["expected_local_image_epoch"] == 7


@pytest.mark.asyncio
async def test_submit_input_rejects_images_when_client_capability_is_unknown() -> None:
    class UnknownCapabilityClient:
        async def start_turn(self, *args, **kwargs):
            raise AssertionError("unknown capability must fail before submission")

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    backend = CodexBackend(
        client=UnknownCapabilityClient(),
        store=store,
        service_name="imcodex-test",
    )
    image = InboundAttachment("image", "image/png", "/tmp/inbound.png", 123)

    with pytest.raises(AppServerError, match="cannot read bridge-local image paths"):
        await backend.submit_input("qq", "conv-1", "", (image,))


@pytest.mark.asyncio
async def test_submit_input_steers_active_turn_with_caption_then_image() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = MultimodalClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    image = InboundAttachment("image", "image/webp", "/tmp/inbound.webp", 321)

    submission = await backend.submit_input("qq", "conv-1", "/status", (image,))

    assert submission.kind == "steer"
    assert client.steer_turn_calls == [
        {
            "thread_id": "thr_1",
            "turn_id": "turn_1",
            "input_items": [
                {"type": "text", "text": "/status"},
                {"type": "localImage", "path": "/tmp/inbound.webp"},
            ],
        }
    ]
    assert client.start_turn_calls == []


@pytest.mark.asyncio
async def test_submit_input_steers_image_only_turn_with_display_text() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = MultimodalClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    image = InboundAttachment("image", "image/png", "/tmp/inbound.png", 123)

    submission = await backend.submit_input("qq", "conv-1", "", (image,))

    assert submission.kind == "steer"
    assert client.steer_turn_calls == [
        {
            "thread_id": "thr_1",
            "turn_id": "turn_1",
            "input_items": [
                {"type": "text", "text": "[Image]"},
                {"type": "localImage", "path": "/tmp/inbound.png"},
            ],
        }
    ]
    assert client.start_turn_calls == []


@pytest.mark.asyncio
async def test_stale_multimodal_steer_falls_back_without_losing_image() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_stale", "inProgress")
    client = MultimodalClient(stale_steer=True)
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    image = InboundAttachment("image", "image/jpeg", "/tmp/inbound.jpg", 456)
    expected_input = [
        {"type": "text", "text": "inspect this"},
        {"type": "localImage", "path": "/tmp/inbound.jpg"},
    ]

    submission = await backend.submit_input("qq", "conv-1", "inspect this", (image,))

    assert submission.kind == "start"
    assert client.steer_turn_calls[0]["input_items"] == expected_input
    assert client.start_turn_calls == [
        {
            "thread_id": "thr_1",
            "input_items": expected_input,
            "summary": "concise",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["unknown thread", "no rollout found for thread id thr_old"])
async def test_interrupt_turn_treats_stale_thread_errors_as_local_cleanup(
    message: str,
) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_old")
    store.note_active_turn("thr_old", "turn_1", "inProgress")
    client = InterruptStaleClient(message)
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    interrupted = await backend.interrupt_turn("thr_old", "turn_1")

    assert interrupted is False
    assert store.get_active_turn("thr_old") is None
    assert store.list_pending_terminal_deliveries() == []
    assert client.interrupt_calls == [{"thread_id": "thr_old", "turn_id": "turn_1"}]


@pytest.mark.asyncio
async def test_interrupt_never_consumes_already_staged_terminal_delivery() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_old")
    store.note_active_turn("thr_old", "turn_1", "inProgress")
    store.stage_terminal_delivery(
        thread_id="thr_old",
        turn_id="turn_1",
        message={
            "channel_id": "qq",
            "conversation_id": "conv-1",
            "message_type": "turn_result",
            "text": "Final result already projected",
            "request_id": None,
            "metadata": {"delivery_id": "stable-1"},
        },
    )
    client = InterruptStaleClient("no active turn")
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.interrupt_turn("thr_old", "turn_1")

    pending = store.list_pending_terminal_deliveries()
    assert len(pending) == 1
    assert pending[0].message is not None
    assert pending[0].message["text"] == "Final result already projected"


@pytest.mark.asyncio
async def test_rehydrate_bound_threads_resumes_all_known_bindings() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.bind_thread_with_cwd("debug", "conv-2", "thr_2", r"D:\desktop\imcodex")
    client = RehydrateClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    summary = await backend.rehydrate_bound_threads()

    assert client.resume_calls == [
        {"thread_id": "thr_1", "service_name": "imcodex-test"},
        {"thread_id": "thr_2", "service_name": "imcodex-test"},
    ]
    assert store.get_thread_snapshot("thr_1").preview == "Recovered thread"
    assert store.get_thread_snapshot("thr_2").preview == "Recovered thread"
    assert summary == {
        "summary": {"total": 2, "succeeded": 2, "failed": 0, "unverified": 0},
        "recoveredTurns": [],
        "discardedTurns": [],
    }


@pytest.mark.asyncio
async def test_rehydrate_bound_threads_clears_active_turn_when_native_thread_is_idle() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = RehydrateClient(status="idle")
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    summary = await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None
    assert summary == {
        "summary": {"total": 1, "succeeded": 0, "failed": 0, "unverified": 1},
        "recoveredTurns": [],
        "discardedTurns": [{"threadId": "thr_1", "turnId": "turn_1"}],
    }


@pytest.mark.asyncio
async def test_rehydrate_bound_threads_returns_terminal_turn_completed_during_disconnect() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    terminal_turn = {
        "id": "turn_1",
        "status": "completed",
        "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Recovered"}],
    }
    client = RehydrateClient(status="idle", turns=[terminal_turn])
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None
    assert store.is_turn_suppressed("thr_1", "turn_1") is True
    assert result == {
        "summary": {"total": 1, "succeeded": 1, "failed": 0, "unverified": 0},
        "recoveredTurns": [{"threadId": "thr_1", "turn": terminal_turn}],
        "discardedTurns": [],
    }


@pytest.mark.asyncio
async def test_rehydrate_recovers_watched_turn_after_full_process_restart(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    before_restart = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    before_restart.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    before_restart.note_active_turn("thr_1", "turn_1", "inProgress")
    await before_restart.flush_pending_writes()

    store = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    terminal_turn = {
        "id": "turn_1",
        "status": "completed",
        "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Recovered"}],
    }
    client = RehydrateClient(status="idle", turns=[terminal_turn])
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None
    assert [item.turn_id for item in store.list_pending_terminal_deliveries("thr_1")] == [
        "turn_1"
    ]
    assert result["recoveredTurns"] == [{"threadId": "thr_1", "turn": terminal_turn}]


@pytest.mark.asyncio
async def test_rehydrate_failure_discards_unverified_active_turn_but_keeps_binding() -> None:
    class FailingRehydrateClient:
        async def resume_thread(self, **_params):
            raise AppServerError("temporarily unavailable")

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    backend = CodexBackend(
        client=FailingRehydrateClient(),
        store=store,
        service_name="imcodex-test",
    )

    summary = await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None
    assert store.get_binding("qq", "conv-1").thread_id == "thr_1"
    assert summary == {
        "summary": {"total": 1, "succeeded": 0, "failed": 1, "unverified": 0},
        "recoveredTurns": [],
        "discardedTurns": [{"threadId": "thr_1", "turnId": "turn_1"}],
    }


@pytest.mark.asyncio
async def test_stale_native_thread_keeps_already_staged_terminal_delivery() -> None:
    class StaleThreadClient:
        async def resume_thread(self, **_params):
            raise AppServerError("unknown thread")

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_stale")
    store.stage_terminal_delivery(
        thread_id="thr_stale",
        turn_id="turn_1",
        message={
            "channel_id": "qq",
            "conversation_id": "conv-1",
            "message_type": "turn_result",
            "text": "Still deliver this",
            "request_id": None,
            "metadata": {"delivery_id": "stable-1"},
        },
    )
    backend = CodexBackend(
        client=StaleThreadClient(),
        store=store,
        service_name="imcodex-test",
    )

    await backend.rehydrate_bound_threads()

    assert store.get_binding("qq", "conv-1").thread_id is None
    assert [item.turn_id for item in store.list_pending_terminal_deliveries()] == ["turn_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thread_payload",
    [
        None,
        {"id": "thr_1", "turns": [{"id": "turn_1", "status": "inProgress"}]},
        {
            "id": "thr_other",
            "status": "inProgress",
            "turns": [{"id": "turn_1", "status": "inProgress"}],
        },
        {"id": "thr_1", "status": {"type": "active"}, "turns": []},
    ],
)
async def test_rehydrate_unverifiable_payload_discards_cached_active_turn(
    thread_payload,
) -> None:
    class UnverifiableClient:
        async def resume_thread(self, **_params):
            return {"thread": thread_payload}

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    backend = CodexBackend(
        client=UnverifiableClient(),
        store=store,
        service_name="imcodex-test",
    )

    summary = await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") is None
    assert store.get_binding("qq", "conv-1").thread_id == "thr_1"
    assert summary == {
        "summary": {"total": 1, "succeeded": 0, "failed": 0, "unverified": 1},
        "recoveredTurns": [],
        "discardedTurns": [{"threadId": "thr_1", "turnId": "turn_1"}],
    }


@pytest.mark.asyncio
async def test_rehydrate_replaces_cached_turn_with_verified_native_active_turn() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_old", "inProgress")
    client = RehydrateClient(
        status="inProgress",
        turns=[{"id": "turn_native", "status": "inProgress"}],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") == ("turn_native", "inProgress")
    assert store.is_turn_suppressed("thr_1", "turn_old") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "inProgress",
        "in_progress",
        "running",
        "working",
        {"type": "active", "activeFlags": []},
    ],
)
async def test_rehydrate_bound_threads_keeps_active_turn_when_native_thread_is_active(
    status: object,
) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "conv-1", "thr_1", r"D:\desktop\imcodex")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    client = RehydrateClient(
        status=status,
        turns=[{"id": "turn_1", "status": "inProgress"}],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.rehydrate_bound_threads()

    assert store.get_active_turn("thr_1") == ("turn_1", "inProgress")


class SettingsClient:
    def __init__(
        self,
        *,
        config: dict | None = None,
        models: list[dict] | None = None,
        profiles: list[dict] | None = None,
        requirements: dict | None = None,
        layers: list[dict] | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.models = list(models or [])
        self.profiles = list(
            profiles
            or [
                {"id": ":workspace", "allowed": True},
                {"id": ":read-only", "allowed": True},
                {"id": ":danger-full-access", "allowed": True},
            ]
        )
        self.requirements = requirements
        self.layers = list(layers or [])
        self.read_calls: list[dict] = []
        self.write_calls: list[dict] = []
        self.batch_calls: list[dict] = []
        self.profile_calls: list[dict] = []
        self.model_calls: list[dict] = []
        self.requirements_calls = 0

    async def read_config(self, *, include_layers: bool = False, cwd: str | None = None):
        self.read_calls.append({"include_layers": include_layers, "cwd": cwd})
        return {
            "config": dict(self.config),
            "origins": {},
            "layers": list(self.layers) if include_layers else None,
        }

    async def list_models(self, params: dict | None = None):
        self.model_calls.append(dict(params or {}))
        return {"data": list(self.models), "nextCursor": None}

    async def list_permission_profiles(self, params: dict):
        self.profile_calls.append(params)
        return {"data": list(self.profiles), "nextCursor": None}

    async def read_config_requirements(self):
        self.requirements_calls += 1
        return {"requirements": self.requirements}

    async def write_config_value(
        self,
        *,
        key_path: str,
        value: object,
        merge_strategy: str = "replace",
    ):
        call = {"key_path": key_path, "value": value, "merge_strategy": merge_strategy}
        self.write_calls.append(call)
        return {"status": "ok"}

    async def batch_write_config(
        self,
        *,
        edits: list[dict],
        reload_user_config: bool = False,
        expected_version: str | None = None,
        file_path: str | None = None,
    ):
        call = {"edits": edits, "reload_user_config": reload_user_config}
        if expected_version is not None:
            call["expected_version"] = expected_version
        if file_path is not None:
            call["file_path"] = file_path
        self.batch_calls.append(call)
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_reasoning_options_follow_selected_native_model_and_reload_config_stack() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-current"},
        models=[
            {
                "id": "gpt-current",
                "displayName": "GPT Current",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast"},
                    {"reasoningEffort": "ultra", "description": "Deep"},
                ],
            }
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    options = await backend.read_reasoning_options("qq", "conv-1")
    await backend.set_reasoning_effort("qq", "conv-1", "ultra")

    assert options["reasoningOptionsSource"] == "native"
    assert options["selectedModel"] == "gpt-current"
    assert options["reasoningEfforts"] == [
        {"reasoningEffort": "low", "description": "Fast"},
        {"reasoningEffort": "ultra", "description": "Deep"},
    ]
    assert client.batch_calls == [
        {
            "edits": [
                {
                    "keyPath": "model_reasoning_effort",
                    "value": "ultra",
                    "mergeStrategy": "replace",
                }
            ],
            "reload_user_config": True,
        }
    ]


@pytest.mark.asyncio
async def test_reasoning_command_uses_managed_model_and_rejects_managed_effort_write() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-user", "model_reasoning_effort": "low"},
        models=[
            {
                "id": "gpt-managed",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            }
        ],
        requirements={
            "models": {
                "newThread": {
                    "model": "gpt-managed",
                    "modelReasoningEffort": "high",
                }
            }
        },
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    options = await backend.read_reasoning_options("qq", "conv-1")

    assert options["selectedModel"] == "gpt-managed"
    assert options["effectiveConfig"]["model_reasoning_effort"] == "high"
    with pytest.raises(AppServerError, match="managed by Codex requirements"):
        await backend.set_reasoning_effort("qq", "conv-1", "high")
    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_global_settings_reads_native_options_without_creating_a_binding() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    layers = [
        {
            "name": {"type": "user", "file": "/tmp/codex/config.toml"},
            "version": "user-v7",
            "config": {"model": "gpt-admin"},
        }
    ]
    client = SettingsClient(
        config={"model": "gpt-admin", "model_reasoning_effort": "high"},
        layers=layers,
        models=[
            {
                "id": "gpt-admin",
                "displayName": "GPT Admin",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "Deep"},
                ],
            }
        ],
        profiles=[{"id": ":workspace", "allowed": True}],
        requirements={"allowedPermissionProfiles": {":workspace": True}},
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.read_global_settings()

    assert result["config"] == {"model": "gpt-admin", "model_reasoning_effort": "high"}
    assert result["layers"] == layers
    assert result["models"] == client.models
    assert result["selectedModel"] == "gpt-admin"
    assert result["reasoningEfforts"] == [
        {"reasoningEffort": "medium", "description": "Balanced"},
        {"reasoningEffort": "high", "description": "Deep"},
    ]
    assert result["profiles"] == [{"id": ":workspace", "allowed": True}]
    assert result["requirements"] == {"allowedPermissionProfiles": {":workspace": True}}
    assert result["nativeProfilesSupported"] is True
    assert client.read_calls == [{"include_layers": True, "cwd": None}]
    assert client.model_calls == [{"includeHidden": True}]
    assert client.profile_calls == [{}]
    assert client.requirements_calls == 1
    assert store.iter_bindings() == []


@pytest.mark.asyncio
async def test_global_settings_projects_managed_new_thread_defaults_from_codex() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={
            "model": "gpt-user",
            "model_reasoning_effort": "low",
            "service_tier": "default",
            "default_permissions": ":workspace",
            "approval_policy": "never",
        },
        models=[
            {
                "id": "gpt-managed",
                "hidden": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority", "name": "Fast"}],
            }
        ],
        requirements={
            "models": {
                "newThread": {
                    "model": "gpt-managed",
                    "modelReasoningEffort": "high",
                    "serviceTier": "priority",
                }
            },
            "defaultPermissions": ":read-only",
        },
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.read_global_settings()

    assert result["config"]["model"] == "gpt-user"
    assert result["effectiveGlobalConfig"] == {
        **result["config"],
        "model": "gpt-managed",
        "model_reasoning_effort": "high",
        "service_tier": "priority",
        "default_permissions": ":read-only",
    }
    assert result["managedSettings"] == ["model", "reasoningEffort", "fast", "permissionMode"]
    assert result["selectedModel"] == "gpt-managed"
    assert client.model_calls == [{"includeHidden": True}]


@pytest.mark.asyncio
async def test_global_managed_model_default_cannot_be_overridden() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(requirements={"models": {"newThread": {"model": "gpt-managed"}}})
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="model is managed"):
        await backend.set_global_model("gpt-user")

    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_global_personality_rejects_model_without_native_capability() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-mini"},
        models=[{"id": "gpt-mini", "displayName": "GPT Mini", "supportsPersonality": False}],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="not supported by GPT Mini"):
        await backend.set_global_personality("friendly")

    await backend.set_global_personality(None)
    assert len(client.batch_calls) == 1


@pytest.mark.asyncio
async def test_personality_feature_requirement_blocks_selection_but_allows_reset() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-capable", "personality": "friendly"},
        models=[
            {"id": "gpt-capable", "supportsPersonality": True},
            {"id": "gpt-no-personality", "supportsPersonality": False},
        ],
        requirements={"features": {"personality": False}},
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    options = await backend.read_personality_options("qq", "conv-1")
    assert options["personalityAvailable"] is False

    with pytest.raises(AppServerError, match="disabled by native Codex feature requirements"):
        await backend.set_personality("qq", "conv-1", "friendly")
    with pytest.raises(AppServerError, match="disabled by native Codex feature requirements"):
        await backend.set_global_preferences({"personality": "friendly"})

    await backend.set_global_preferences({"model": "gpt-no-personality"})
    assert client.batch_calls[-1]["edits"] == [
        {"keyPath": "model", "value": "gpt-no-personality", "mergeStrategy": "replace"}
    ]

    await backend.set_personality("qq", "conv-1", None)
    assert client.batch_calls[-1]["edits"] == [
        {"keyPath": "personality", "value": None, "mergeStrategy": "replace"}
    ]


@pytest.mark.asyncio
async def test_fast_options_use_native_model_default_and_feature_gate() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-fast"},
        models=[{"id": "gpt-fast", "defaultServiceTier": "priority"}],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.read_fast_options("qq", "conv-1")

    assert result["selectedModelDefaultServiceTier"] == "priority"
    assert result["fastAvailable"] is True
    assert client.model_calls == [{"includeHidden": True}]


@pytest.mark.asyncio
async def test_fast_enable_rejects_native_feature_requirement_but_off_remains_available() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-fast"},
        models=[{"id": "gpt-fast", "serviceTiers": [{"id": "priority"}]}],
        requirements={"featureRequirements": {"fast_mode": False}},
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="disabled by native Codex feature requirements"):
        await backend.set_global_fast_mode(True)
    await backend.set_global_fast_mode(False)

    assert len(client.batch_calls) == 1


@pytest.mark.asyncio
async def test_global_settings_writes_use_optimistic_user_layer_without_binding() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-admin"},
        layers=[
            {
                "name": {"type": "user", "file": "/tmp/codex/config.toml"},
                "version": "user-v9",
                "config": {},
            }
        ],
        models=[
            {
                "id": "gpt-admin",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority", "name": "Fast"}],
                "additionalSpeedTiers": ["fast"],
            }
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.set_global_model("gpt-admin")
    await backend.set_global_reasoning_effort("HIGH")
    await backend.set_global_personality("Friendly")
    await backend.set_global_fast_mode(True)
    permission = await backend.set_global_permission_mode("default")

    assert client.read_calls == [{"include_layers": True, "cwd": None}] * 5
    assert client.requirements_calls == 5
    assert client.batch_calls == [
        {
            "edits": [{"keyPath": "model", "value": "gpt-admin", "mergeStrategy": "replace"}],
            "reload_user_config": False,
            "expected_version": "user-v9",
            "file_path": "/tmp/codex/config.toml",
        },
        {
            "edits": [
                {
                    "keyPath": "model_reasoning_effort",
                    "value": "high",
                    "mergeStrategy": "replace",
                }
            ],
            "reload_user_config": True,
            "expected_version": "user-v9",
            "file_path": "/tmp/codex/config.toml",
        },
        {
            "edits": [
                {
                    "keyPath": "personality",
                    "value": "friendly",
                    "mergeStrategy": "replace",
                }
            ],
            "reload_user_config": True,
            "expected_version": "user-v9",
            "file_path": "/tmp/codex/config.toml",
        },
        {
            "edits": [
                {
                    "keyPath": "service_tier",
                    "value": "priority",
                    "mergeStrategy": "replace",
                },
            ],
            "reload_user_config": False,
            "expected_version": "user-v9",
            "file_path": "/tmp/codex/config.toml",
        },
        {
            "edits": [
                {
                    "keyPath": "default_permissions",
                    "value": ":workspace",
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "approval_policy",
                    "value": "on-request",
                    "mergeStrategy": "replace",
                },
                {"keyPath": "sandbox_mode", "value": None, "mergeStrategy": "replace"},
            ],
            "reload_user_config": True,
            "expected_version": "user-v9",
            "file_path": "/tmp/codex/config.toml",
        },
    ]
    assert permission == {
        "status": "ok",
        "mode": "default",
        "profile": ":workspace",
        "fallback": False,
    }
    assert store.iter_bindings() == []


@pytest.mark.asyncio
async def test_global_preferences_apply_model_transition_atomically_against_candidate_state() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={
            "model": "gpt-old",
            "model_reasoning_effort": "high",
            "personality": "friendly",
            "service_tier": "priority",
        },
        layers=[
            {
                "name": {"type": "user", "file": "/tmp/codex/config.toml"},
                "version": "user-v11",
                "config": {},
            }
        ],
        models=[
            {
                "id": "gpt-old",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "supportsPersonality": True,
                "serviceTiers": [{"id": "priority"}],
            },
            {
                "id": "gpt-new",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                "supportsPersonality": False,
                "serviceTiers": [],
            },
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.set_global_preferences(
        {
            "model": "gpt-new",
            "reasoningEffort": "low",
            "personality": None,
            "fast": False,
        }
    )

    assert client.batch_calls == [
        {
            "edits": [
                {"keyPath": "model", "value": "gpt-new", "mergeStrategy": "replace"},
                {
                    "keyPath": "model_reasoning_effort",
                    "value": "low",
                    "mergeStrategy": "replace",
                },
                {"keyPath": "personality", "value": None, "mergeStrategy": "replace"},
                {"keyPath": "service_tier", "value": "default", "mergeStrategy": "replace"},
            ],
            "reload_user_config": True,
            "expected_version": "user-v11",
            "file_path": "/tmp/codex/config.toml",
        }
    ]


@pytest.mark.asyncio
async def test_global_preferences_reject_model_incompatible_with_managed_effort() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        models=[
            {
                "id": "gpt-low",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            }
        ],
        requirements={"models": {"newThread": {"modelReasoningEffort": "high"}}},
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="reasoning effort high is not supported"):
        await backend.set_global_preferences({"model": "gpt-low"})

    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_global_fast_mode_disables_with_native_default_tier_only() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        layers=[
            {
                "name": {"type": "user", "file": "/tmp/codex/config.toml"},
                "version": "user-v10",
                "config": {"service_tier": "priority"},
            }
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.set_global_fast_mode(False)

    assert client.batch_calls == [
        {
            "edits": [
                {
                    "keyPath": "service_tier",
                    "value": "default",
                    "mergeStrategy": "replace",
                },
            ],
            "reload_user_config": False,
            "expected_version": "user-v10",
            "file_path": "/tmp/codex/config.toml",
        }
    ]


@pytest.mark.asyncio
async def test_atomic_preferences_keep_fast_off_available_without_model_catalog() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(config={"service_tier": "priority"}, models=[])
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.set_global_preferences({"fast": False})

    assert client.model_calls == []
    assert client.batch_calls[0]["edits"] == [
        {"keyPath": "service_tier", "value": "default", "mergeStrategy": "replace"}
    ]


@pytest.mark.asyncio
async def test_global_fast_mode_enable_rejects_unsupported_native_model() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-mini"},
        models=[
            {
                "id": "gpt-mini",
                "serviceTiers": [],
                "additionalSpeedTiers": [],
            }
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="not available for gpt-mini"):
        await backend.set_global_fast_mode(True)

    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_conversation_fast_write_validates_the_project_effective_model() -> None:
    class ProjectSettingsClient(SettingsClient):
        async def read_config(self, *, include_layers: bool = False, cwd: str | None = None):
            self.read_calls.append({"include_layers": include_layers, "cwd": cwd})
            model = "gpt-project" if cwd == "/tmp/project" else "gpt-global"
            return {"config": {"model": model}, "origins": {}, "layers": None}

    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", "/tmp/project")
    client = ProjectSettingsClient(
        models=[
            {"id": "gpt-global", "serviceTiers": [{"id": "priority"}]},
            {"id": "gpt-project", "serviceTiers": []},
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="not available for gpt-project"):
        await backend.set_fast_mode("qq", "conv-1", True)

    assert client.read_calls == [{"include_layers": False, "cwd": "/tmp/project"}]
    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_global_reasoning_rejects_effort_not_advertised_by_native_model() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        config={"model": "gpt-admin"},
        models=[
            {
                "id": "gpt-admin",
                "displayName": "GPT Admin",
                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
            }
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="available efforts: medium"):
        await backend.set_global_reasoning_effort("high")

    assert client.batch_calls == []
    assert store.iter_bindings() == []


@pytest.mark.asyncio
async def test_reasoning_write_rejects_effort_not_advertised_by_selected_model() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        models=[
            {
                "id": "gpt-default",
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": "Balanced"}],
            }
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="available efforts: medium"):
        await backend.set_reasoning_effort("qq", "conv-1", "minimal")

    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_personality_write_reloads_native_config_stack() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.set_default_personality("pragmatic")

    assert client.batch_calls == [
        {
            "edits": [
                {
                    "keyPath": "personality",
                    "value": "pragmatic",
                    "mergeStrategy": "replace",
                }
            ],
            "reload_user_config": True,
        }
    ]


@pytest.mark.asyncio
async def test_permission_bootstrap_seeds_documented_full_access_only_when_unset() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        layers=[
            {
                "name": {"type": "user", "file": "/tmp/codex/config.toml"},
                "version": "user-v1",
                "config": {},
            }
        ]
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.ensure_default_permission_mode(1)

    assert result["changed"] is True
    assert client.read_calls == [{"include_layers": True, "cwd": None}]
    assert client.batch_calls == [
        {
            "edits": [
                {
                    "keyPath": "default_permissions",
                    "value": ":danger-full-access",
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "approval_policy",
                    "value": "never",
                    "mergeStrategy": "replace",
                },
                {"keyPath": "sandbox_mode", "value": None, "mergeStrategy": "replace"},
            ],
            "reload_user_config": True,
            "expected_version": "user-v1",
            "file_path": "/tmp/codex/config.toml",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"default_permissions": ":workspace"},
        {"approval_policy": "on-request"},
        {"sandbox_mode": "read-only"},
    ],
)
async def test_permission_bootstrap_preserves_any_existing_native_permission_choice(
    config: dict,
) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(config=config)
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.ensure_default_permission_mode(1)

    assert result["changed"] is False
    assert client.profile_calls == []
    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_permission_bootstrap_preserves_managed_default_and_restrictions() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(
        requirements={
            "defaultPermissions": ":read-only",
            "allowedPermissionProfiles": {
                ":read-only": True,
                ":danger-full-access": False,
            },
        },
        profiles=[
            {"id": ":read-only", "allowed": True},
            {"id": ":danger-full-access", "allowed": False},
        ],
    )
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.ensure_default_permission_mode(1)

    assert result == {
        "changed": False,
        "reason": "Codex requirements define the permission default :read-only",
    }
    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_permission_profile_definitions_do_not_count_as_a_selected_permission_mode() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = SettingsClient(config={"permissions": {"team": {"sandbox": "read-only"}}})
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    result = await backend.ensure_default_permission_mode(1)

    assert result["changed"] is True
    assert client.batch_calls[0]["edits"][0]["value"] == ":danger-full-access"


class FailingModelCatalogClient(SettingsClient):
    async def list_models(self, params: dict | None = None):
        self.model_calls.append(dict(params or {}))
        raise AppServerError("model catalog unavailable")


@pytest.mark.asyncio
async def test_reasoning_fallback_rejects_values_outside_compatibility_choices() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = FailingModelCatalogClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="available efforts"):
        await backend.set_reasoning_effort("qq", "conv-1", "banana")

    assert client.batch_calls == []


class PaginatedModelCatalogClient(SettingsClient):
    async def list_models(self, params: dict | None = None):
        params = dict(params or {})
        self.model_calls.append(params)
        if params.get("cursor") == "page-2":
            return {
                "data": [
                    {
                        "id": "gpt-selected",
                        "displayName": "GPT Selected",
                        "supportedReasoningEfforts": [{"reasoningEffort": "ultra", "description": "Deep"}],
                        "defaultReasoningEffort": "ultra",
                    }
                ],
                "nextCursor": None,
            }
        return {
            "data": [
                {
                    "id": "gpt-first",
                    "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": "Balanced"}],
                }
            ],
            "nextCursor": "page-2",
        }


@pytest.mark.asyncio
async def test_reasoning_catalog_follows_pagination_to_find_selected_model() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = PaginatedModelCatalogClient(config={"model": "gpt-selected"})
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    options = await backend.read_reasoning_options("qq", "conv-1")

    assert client.model_calls == [{}, {"cursor": "page-2"}]
    assert options["selectedModel"] == "gpt-selected"
    assert options["reasoningEfforts"] == [
        {"reasoningEffort": "ultra", "description": "Deep"},
    ]


class RepeatingPermissionCursorClient(SettingsClient):
    async def list_permission_profiles(self, params: dict):
        self.profile_calls.append(params)
        return {
            "data": [{"id": ":workspace", "allowed": True}],
            "nextCursor": "same-cursor",
        }


@pytest.mark.asyncio
async def test_permission_profile_catalog_rejects_repeated_pagination_cursor() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    client = RepeatingPermissionCursorClient(config={"default_permissions": ":workspace"})
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="repeated pagination cursor"):
        await backend.read_permission_options("qq", "conv-1")

    assert client.profile_calls == [{}, {"cursor": "same-cursor"}]

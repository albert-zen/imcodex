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
    assert client.start_thread_calls == [
        {"cwd": r"D:\desktop\imcodex", "service_name": "imcodex-test"},
    ]
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
        {"thread_id": "thr_1", "service_name": "imcodex-test"},
        {"thread_id": "thr_2", "service_name": "imcodex-test"},
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
        self.batch_calls: list[dict] = []
        self.profile_calls: list[dict] = []
        self.model_calls: list[dict] = []

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
        return {"requirements": self.requirements}

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
            "edits": [{"keyPath": "personality", "value": "pragmatic", "mergeStrategy": "replace"}],
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
                {"keyPath": "approval_policy", "value": "never", "mergeStrategy": "replace"},
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
async def test_permission_bootstrap_preserves_any_existing_native_permission_choice(config: dict) -> None:
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
            "allowedPermissionProfiles": {":read-only": True, ":danger-full-access": False},
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

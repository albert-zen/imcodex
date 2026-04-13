from __future__ import annotations

import asyncio

import pytest

from imcodex.appserver import AppServerError, CodexBackend, StaleThreadBindingError
from imcodex.store import ConversationStore


class FakeClient:
    def __init__(self) -> None:
        self.thread_resumes: list[dict] = []
        self.thread_starts: list[dict] = []
        self.thread_lists: list[dict] = []
        self.thread_reads: list[dict] = []
        self.turn_starts: list[dict] = []
        self.turn_steers: list[dict] = []
        self.turn_interrupts: list[dict] = []
        self.replies: list[tuple[str, dict]] = []
        self.fail_thread_ids: set[str] = set()
        self.fail_resume_ids: set[str] = set()
        self.resume_errors: dict[str, str] = {}
        self.resume_results: dict[str, dict[str, str]] = {}
        self.list_result: list[dict] = []
        self.read_results: dict[str, dict] = {}
        self.fail_steer_messages: list[str] = []
        self.fail_interrupt = False

    async def resume_thread(self, **params):
        self.thread_resumes.append(params)
        if params["thread_id"] in self.resume_errors:
            raise AppServerError(self.resume_errors[params["thread_id"]])
        if params["thread_id"] in self.fail_resume_ids:
            raise AppServerError("invalid request")
        result = self.resume_results.get(params["thread_id"])
        if result is not None:
            return {
                "thread": {
                    "id": result.get("id", params["thread_id"]),
                    "preview": result.get("preview", ""),
                    "status": {"type": "idle"},
                }
            }
        return {
            "thread": {
                "id": params["thread_id"],
                "preview": "",
                "status": {"type": "idle"},
            }
        }

    async def start_thread(self, **params):
        self.thread_starts.append(params)
        return {"thread": {"id": "thr_new", "preview": "", "status": {"type": "idle"}}}

    async def list_threads(self, **params):
        self.thread_lists.append(params)
        return {"threads": list(self.list_result)}

    async def read_thread(self, thread_id: str):
        self.thread_reads.append({"thread_id": thread_id})
        return {"thread": dict(self.read_results.get(thread_id, {"id": thread_id, "status": {"type": "idle"}}))}

    async def start_turn(self, **params):
        self.turn_starts.append(params)
        if params["thread_id"] in self.fail_thread_ids:
            self.fail_thread_ids.remove(params["thread_id"])
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
        {
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]


@pytest.mark.asyncio
async def test_ensure_thread_resumes_bound_thread_when_present() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    store.set_active_thread("demo", "conv-1", "thr_existing")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.ensure_thread("demo", "conv-1")

    assert thread_id == "thr_existing"
    assert client.thread_resumes == [
        {
            "thread_id": "thr_existing",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.thread_starts == []


@pytest.mark.asyncio
async def test_ensure_thread_marks_binding_stale_when_resume_fails() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    store.set_active_thread("demo", "conv-1", "thr_existing")
    client = FakeClient()
    client.fail_resume_ids.add("thr_existing")
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(StaleThreadBindingError, match="thr_existing"):
        await backend.ensure_thread("demo", "conv-1")

    assert client.thread_resumes == [
        {
            "thread_id": "thr_existing",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.thread_starts == []
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_thread_id == "thr_existing"
    assert store.get_thread("thr_existing").status == "stale"


@pytest.mark.asyncio
async def test_attach_thread_resumes_unknown_thread_in_selected_working_directory() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.attach_thread("demo", "conv-1", "thr_external")

    assert thread_id == "thr_external"
    assert client.thread_resumes == [
        {
            "thread_id": "thr_external",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert store.get_binding("demo", "conv-1").active_thread_id == "thr_external"


@pytest.mark.asyncio
async def test_attach_thread_persists_across_restart_and_reuses_resumed_thread(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    project = store.ensure_project("D:/repo/app")
    store.set_active_project("demo", "conv-1", project.project_id)
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.attach_thread("demo", "conv-1", "thr_external")

    reloaded = ConversationStore(clock=lambda: 2.0, state_path=state_path)
    resumed_client = FakeClient()
    resumed_backend = CodexBackend(client=resumed_client, store=reloaded, service_name="imcodex-test")

    thread_id = await resumed_backend.ensure_thread("demo", "conv-1")

    assert thread_id == "thr_external"
    assert resumed_client.thread_resumes == [
        {
            "thread_id": "thr_external",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]


@pytest.mark.asyncio
async def test_attach_thread_prefers_selected_working_directory_over_known_thread_record() -> None:
    store = make_store()
    alpha = store.record_thread("thr_known", cwd="D:/repo/alpha", preview="old")
    beta = store.ensure_project("D:/repo/beta")
    store.set_active_project("demo", "conv-1", beta.project_id)
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.attach_thread("demo", "conv-1", alpha.thread_id)

    assert thread_id == "thr_known"
    assert client.thread_resumes == [
        {
            "thread_id": "thr_known",
            "cwd": "D:/repo/beta",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    binding = store.get_binding("demo", "conv-1")
    assert binding.active_project_id == beta.project_id
    assert binding.active_thread_id == "thr_known"


@pytest.mark.asyncio
async def test_attach_thread_refreshes_preview_for_known_thread() -> None:
    store = make_store()
    store.record_thread("thr_known", cwd="D:/repo/app", preview="")
    store.set_active_thread("demo", "conv-1", "thr_known")
    client = FakeClient()
    client.resume_results["thr_known"] = {"preview": "Imported thread"}
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.attach_thread("demo", "conv-1", "thr_known")

    assert store.thread_label("thr_known") == "Imported thread"


@pytest.mark.asyncio
async def test_ensure_thread_prefers_selected_cwd_when_project_alias_is_missing() -> None:
    store = make_store()
    binding = store.set_selected_cwd("demo", "conv-1", "D:/repo/alt")
    binding.active_project_id = None
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    thread_id = await backend.ensure_thread("demo", "conv-1")

    assert thread_id == "thr_new"
    assert client.thread_starts == [
        {
            "cwd": "D:/repo/alt",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]


@pytest.mark.asyncio
async def test_list_threads_imports_native_thread_metadata() -> None:
    store = make_store()
    client = FakeClient()
    client.list_result = [
        {
            "id": "thr_native_1",
            "name": "Investigate alpha",
            "cwd": "D:/repo/app",
            "path": "D:/repo/app",
            "preview": "Inspect failing tests",
            "status": "idle",
        },
        {
            "id": "thr_native_2",
            "name": "Fix beta",
            "cwd": "D:/repo/other",
            "path": "D:/repo/other",
            "preview": "Ready",
            "status": "completed",
        },
    ]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    snapshots = await backend.list_threads("demo", "conv-1")

    assert [snapshot.thread_id for snapshot in snapshots] == ["thr_native_1"]
    assert snapshots[0].name == "Investigate alpha"
    assert store.get_thread("thr_native_1").name == "Investigate alpha"
    assert client.thread_lists == [{}]


@pytest.mark.asyncio
async def test_list_threads_can_skip_cwd_filter_for_all_threads() -> None:
    store = make_store()
    client = FakeClient()
    client.list_result = [
        {
            "id": "thr_native_1",
            "name": "Investigate alpha",
            "cwd": "D:/repo/app",
            "path": "D:/repo/app",
            "preview": "Inspect failing tests",
            "status": "idle",
        },
        {
            "id": "thr_native_2",
            "name": "Fix beta",
            "cwd": "D:/repo/other",
            "path": "D:/repo/other",
            "preview": "Ready",
            "status": "completed",
        },
    ]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    snapshots = await backend.list_threads("demo", "conv-1", include_all=True)

    assert [snapshot.thread_id for snapshot in snapshots] == ["thr_native_1", "thr_native_2"]


@pytest.mark.asyncio
async def test_read_thread_imports_native_thread_metadata() -> None:
    store = make_store()
    client = FakeClient()
    client.read_results["thr_native"] = {
        "id": "thr_native",
        "name": "Investigate alpha",
        "cwd": "D:/repo/app",
        "path": "D:/repo/app",
        "preview": "Inspect failing tests",
        "status": "completed",
    }
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    snapshot = await backend.read_thread("demo", "conv-1", "thr_native")

    assert snapshot is not None
    assert snapshot.thread_id == "thr_native"
    assert snapshot.status == "completed"
    assert store.get_thread("thr_native").name == "Investigate alpha"
    assert client.thread_reads == [{"thread_id": "thr_native"}]


@pytest.mark.asyncio
async def test_start_turn_tracks_active_turn() -> None:
    store = make_store()
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    turn_id = await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert turn_id == "turn_1"
    assert store.get_binding("demo", "conv-1").active_turn_id == "turn_1"
    assert client.turn_starts == [
        {
            "thread_id": "thr_new",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        }
    ]


@pytest.mark.asyncio
async def test_autonomous_permission_profile_flows_into_native_approval_policy() -> None:
    store = make_store()
    store.set_permission_profile("demo", "conv-1", "autonomous")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.ensure_thread("demo", "conv-1")
    await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert client.thread_starts == [
        {
            "cwd": "D:/repo/app",
            "approval_policy": "never",
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_new",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": None,
            "approval_policy": "never",
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        }
    ]


@pytest.mark.asyncio
async def test_review_permission_profile_flows_into_native_approval_and_sandbox_policy() -> None:
    store = make_store()
    store.set_permission_profile("demo", "conv-1", "review")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.ensure_thread("demo", "conv-1")
    await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert client.thread_starts == [
        {
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_new",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        }
    ]


@pytest.mark.asyncio
async def test_selected_model_flows_into_native_thread_and_turn_requests() -> None:
    store = make_store()
    store.set_model_override("demo", "conv-1", "gpt-5.4")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    await backend.ensure_thread("demo", "conv-1")
    await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert client.thread_starts == [
        {
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": "gpt-5.4",
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_new",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": "gpt-5.4",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        }
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
async def test_interrupt_turn_clears_only_pending_requests_for_active_turn() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="9",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        thread_id="thr_existing",
        turn_id="turn_existing",
    )
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="10",
        kind="approval",
        summary="Approve later command",
        payload={"command": "git status"},
        thread_id="thr_existing",
        turn_id="turn_other",
    )

    await backend.interrupt_active_turn("demo", "conv-1")

    assert store.get_pending_request("9") is None
    assert store.get_pending_request("10") is not None
    assert binding.pending_request_ids == ["10"]


@pytest.mark.asyncio
async def test_start_turn_retries_with_resumed_thread_when_bound_thread_is_stale() -> None:
    store = make_store()
    store.record_thread("thr_stale", cwd="D:/repo/app", preview="stale")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_stale"
    binding.known_thread_ids.append("thr_stale")
    client.fail_thread_ids.add("thr_stale")

    turn_id = await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert turn_id == "turn_1"
    assert client.thread_resumes == [
        {
            "thread_id": "thr_stale",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.thread_starts == [
        {
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.turn_starts == [
        {
            "thread_id": "thr_stale",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        },
        {
            "thread_id": "thr_new",
            "text": "Please inspect the repo",
            "cwd": None,
            "model": None,
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        },
    ]
    assert binding.active_thread_id == "thr_new"
    assert binding.active_turn_id == "turn_1"


@pytest.mark.asyncio
async def test_start_turn_surfaces_stale_binding_when_resume_fails_before_restart_recovery() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    store.set_active_thread("demo", "conv-1", "thr_existing")
    client = FakeClient()
    client.fail_resume_ids.add("thr_existing")
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(StaleThreadBindingError, match="thr_existing"):
        await backend.start_turn("demo", "conv-1", "Please inspect the repo")

    assert client.thread_resumes == [
        {
            "thread_id": "thr_existing",
            "cwd": "D:/repo/app",
            "approval_policy": None,
            "sandbox_policy": None,
            "approvals_reviewer": None,
            "model": None,
            "personality": "friendly",
            "service_name": "imcodex-test",
        }
    ]
    assert client.thread_starts == []
    assert client.turn_starts == []
    assert store.get_thread("thr_existing").status == "stale"


@pytest.mark.asyncio
async def test_ensure_thread_preserves_transport_failures_during_resume() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    store.set_active_thread("demo", "conv-1", "thr_existing")
    client = FakeClient()
    client.resume_errors["thr_existing"] = "thread/resume timed out after 15.0s"
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    with pytest.raises(AppServerError, match="timed out"):
        await backend.ensure_thread("demo", "conv-1")

    assert client.thread_starts == []
    assert store.get_thread("thr_existing").status != "stale"


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
            "approvals_reviewer": None,
            "effort": None,
            "summary": "concise",
        }
    ]
    assert binding.active_turn_id == "turn_1"
    assert binding.active_turn_status == "inProgress"


@pytest.mark.asyncio
async def test_start_turn_recovery_clears_only_pending_requests_for_recovered_turn() -> None:
    store = make_store()
    store.record_thread("thr_existing", cwd="D:/repo/app", preview="existing")
    client = FakeClient()
    client.fail_steer_messages = ["invalid request"]
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")
    binding = store.get_binding("demo", "conv-1")
    binding.active_thread_id = "thr_existing"
    binding.active_turn_id = "turn_existing"
    binding.active_turn_status = "inProgress"
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="11",
        kind="approval",
        summary="Approve command",
        payload={"command": "pytest -q"},
        thread_id="thr_existing",
        turn_id="turn_existing",
    )
    store.create_pending_request(
        channel_id="demo",
        conversation_id="conv-1",
        ticket_id="12",
        kind="approval",
        summary="Approve later command",
        payload={"command": "git status"},
        thread_id="thr_existing",
        turn_id="turn_other",
    )

    turn_id = await backend.start_turn("demo", "conv-1", "Actually focus on failing tests first")

    assert turn_id == "turn_1"
    assert store.get_pending_request("11") is None
    assert store.get_pending_request("12") is not None
    assert binding.pending_request_ids == ["12"]


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
            "approvals_reviewer": None,
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

    await backend.reply_to_server_request("demo", "conv-1", "7", {"decision": "accept"})

    assert client.replies == [("99", {"decision": "accept"})]
    pending = store.get_pending_request("7")
    assert pending is not None
    assert pending.resolved_at is None
    assert pending.submitted_resolution == {"decision": "accept"}

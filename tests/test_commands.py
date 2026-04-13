from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from imcodex.bridge import CommandRouter, parse_command
from imcodex.store import ConversationStore


def _state_path(name: str) -> Path:
    return Path.cwd() / f".pytest-state-{name}-{uuid4().hex}.json"


def test_parse_projects_and_switch_commands() -> None:
    assert parse_command("/projects").name == "projects"
    assert parse_command("/project use proj-1").args == ["use", "proj-1"]
    assert parse_command(r"/cwd D:\work\alpha").args == [r"D:\work\alpha"]
    assert parse_command("/threads --all").args == ["--all"]
    assert parse_command("/thread use thr-1").args == ["use", "thr-1"]
    assert parse_command("/thread attach thr-2").args == ["attach", "thr-2"]
    assert parse_command("/new").name == "new"
    assert parse_command("/status").name == "status"
    assert parse_command("/stop").name == "stop"
    assert parse_command("/thread read").args == ["read"]
    assert parse_command("/recover").name == "recover"


def test_parse_approval_and_answer_commands() -> None:
    assert parse_command("/approve T-1").name == "approve"
    assert parse_command("/approve 1 2 3").args == ["1", "2", "3"]
    assert parse_command("/approve-session T-2").name == "approve-session"
    assert parse_command("/deny T-3").name == "deny"
    assert parse_command("/cancel T-4").name == "cancel"
    assert parse_command("/answer T-5 timezone=Asia/Shanghai,UTC+8").name == "answer"
    assert parse_command("/permissions autonomous").args == ["autonomous"]
    assert parse_command("/view verbose").args == ["verbose"]
    assert parse_command("/show commentary").args == ["commentary"]
    assert parse_command("/hide toolcalls").args == ["toolcalls"]
    assert parse_command("/model gpt-5.4").args == ["gpt-5.4"]
    assert parse_command("/requests").name == "requests"
    assert parse_command("/doctor").name == "doctor"
    assert parse_command("/help").name == "help"


def test_router_projects_and_project_switch() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    beta = store.record_thread("thr_2", cwd=r"D:\work\beta", preview="b")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/projects")
    assert response.text.startswith("Legacy project aliases:")
    assert alpha.cwd in response.text
    assert beta.cwd in response.text
    assert alpha.project_id in response.text
    assert beta.project_id in response.text

    response = router.handle("qq", "conv-1", f"/project use {beta.project_id}")
    assert response.action == "project.use"
    assert response.text.startswith("CWD set to ")
    assert beta.cwd in response.text
    assert "Prefer /cwd <path>" in response.text
    assert beta.project_id not in response.text
    assert store.get_binding("qq", "conv-1").active_project_id == beta.project_id
    assert store.get_binding("qq", "conv-1").active_thread_id is None


def test_router_cwd_creates_and_selects_project(tmp_path: Path) -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)
    project_path = tmp_path / "alpha"
    project_path.mkdir()

    response = router.handle("qq", "conv-1", f"/cwd {project_path}")

    binding = store.get_binding("qq", "conv-1")
    project = store.get_project(binding.active_project_id)
    assert response.action == "project.cwd"
    assert str(project_path) in response.text
    assert "project id" not in response.text
    assert response.text.startswith("CWD set to ")
    assert project.cwd == str(project_path)
    assert binding.active_thread_id is None


def test_router_thread_switch_and_status() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed preview")
    store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="")
    store.note_thread_user_message(
        "thr_2",
        "please inspect why the Windows working directory resets after restart",
    )
    router = CommandRouter(store)
    store.set_active_project("qq", "conv-1", alpha.project_id)

    threads = router.handle("qq", "conv-1", "/threads")
    assert threads.action == "threads.query"
    assert threads.include_all is False

    response = router.handle("qq", "conv-1", "/thread use thr_2")
    assert response.action == "thread.use"
    assert "please inspect why the Windows working directory resets..." in response.text
    assert response.text.index("please inspect why the Windows working directory resets...") < response.text.index("thr_2")
    assert store.get_binding("qq", "conv-1").active_thread_id == "thr_2"

    status = router.handle("qq", "conv-1", "/status")
    lines = status.text.splitlines()
    assert lines[0] == f"CWD: {alpha.cwd}"
    assert "Thread: please inspect why the Windows working directory resets..." in status.text
    assert "Thread ID: thr_2" in status.text
    assert "Permission Profile: review" in status.text
    assert "Visibility: standard" in status.text
    assert "Commentary: shown" in status.text
    assert "Tool Calls: hidden" in status.text


def test_router_uses_selected_cwd_when_project_alias_is_missing() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed preview")
    router = CommandRouter(store)
    binding = store.set_selected_cwd("qq", "conv-1", thread.cwd)
    binding.active_project_id = None

    threads = router.handle("qq", "conv-1", "/threads")
    status = router.handle("qq", "conv-1", "/status")
    new_response = router.handle("qq", "conv-1", "/new")

    assert threads.action == "threads.query"
    assert threads.include_all is False
    assert f"CWD: {thread.cwd}" in status.text
    assert new_response.action == "thread.new"
    assert new_response.text == f"Starting a thread in {thread.cwd}."


def test_router_thread_attach_uses_selected_working_directory() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed preview")
    router = CommandRouter(store)
    store.set_active_project("qq", "conv-1", alpha.project_id)

    response = router.handle("qq", "conv-1", "/thread attach thr_external")

    assert response.action == "thread.attach"
    assert response.thread_id == "thr_external"
    assert f"CWD {alpha.cwd}" in response.text


def test_router_thread_attach_does_not_require_preselected_working_directory() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread attach thr_external")

    assert response.action == "thread.attach"
    assert response.thread_id == "thr_external"
    assert response.text == "Attaching thread thr_external."


def test_router_new_stop_and_approval_commands() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    router = CommandRouter(store)
    store.set_active_project("qq", "conv-1", project.project_id)

    new_response = router.handle("qq", "conv-1", "/new")
    assert new_response.action == "thread.new"
    assert store.get_binding("qq", "conv-1").active_thread_id is None

    store.set_active_turn("qq", "conv-1", thread_id="thr_1", turn_id="turn_1", status="in_progress")
    stop_response = router.handle("qq", "conv-1", "/stop")
    assert stop_response.action == "turn.stop"
    assert stop_response.turn_id == "turn_1"

    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="T-9",
        kind="approval",
        summary="Approve shell command",
        payload={"decision": "accept"},
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="T-10",
        kind="question",
        summary="Need values",
        payload={"answers": {}},
    )

    approve = router.handle("qq", "conv-1", "/approve T-9")
    assert approve.action == "approval.accept"
    assert approve.ticket_ids == ["T-9"]

    answer = router.handle("qq", "conv-1", "/answer T-10 timezone=Asia/Shanghai,UTC+8")
    assert answer.action == "request.answer"
    assert answer.answers == {"timezone": ["Asia/Shanghai", "UTC+8"]}


def test_router_new_requires_explicit_working_directory_even_with_single_cached_project() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/new")

    assert response.action == "thread.new.missing_project"
    assert "/cwd <path>" in response.text


def test_router_new_does_not_use_legacy_project_alias_without_selected_cwd() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.ensure_project(r"D:\work\alpha")
    binding = store.get_binding("qq", "conv-1")
    binding.active_project_id = project.project_id
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/new")

    assert response.action == "thread.new.missing_project"
    assert "/cwd <path>" in response.text


def test_router_threads_do_not_use_legacy_project_alias_without_selected_cwd() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.ensure_project(r"D:\work\alpha")
    binding = store.get_binding("qq", "conv-1")
    binding.active_project_id = project.project_id
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/threads")

    assert response.action == "threads.missing_project"
    assert "/cwd <path>" in response.text


def test_router_recover_clears_active_thread_binding_but_preserves_working_directory() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    router = CommandRouter(store)
    store.set_active_thread("qq", "conv-1", thread.thread_id)

    response = router.handle("qq", "conv-1", "/recover")

    binding = store.get_binding("qq", "conv-1")
    assert response.action == "recover"
    assert "Cleared stale thread binding thr_1." in response.text
    assert binding.active_thread_id is None
    assert binding.active_project_id == thread.project_id
    assert binding.selected_cwd == thread.cwd


def test_router_supports_permission_and_visibility_commands() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)

    permissions = router.handle("qq", "conv-1", "/permissions autonomous")
    assert permissions.action == "settings.permissions"
    assert permissions.text == "Permission profile set to autonomous."
    assert store.get_binding("qq", "conv-1").permission_profile == "autonomous"

    view = router.handle("qq", "conv-1", "/view verbose")
    assert view.action == "settings.view"
    assert view.text == "Visibility profile set to verbose."
    binding = store.get_binding("qq", "conv-1")
    assert binding.visibility_profile == "verbose"
    assert binding.show_commentary is True
    assert binding.show_toolcalls is True

    hide_commentary = router.handle("qq", "conv-1", "/hide commentary")
    assert hide_commentary.action == "settings.visibility"
    assert hide_commentary.text == "Commentary messages hidden."
    assert store.get_binding("qq", "conv-1").show_commentary is False

    hide_toolcalls = router.handle("qq", "conv-1", "/hide toolcalls")
    assert hide_toolcalls.action == "settings.visibility"
    assert hide_toolcalls.text == "Tool-call messages hidden."
    assert store.get_binding("qq", "conv-1").show_toolcalls is False

    show_toolcalls = router.handle("qq", "conv-1", "/show toolcalls")
    assert show_toolcalls.action == "settings.visibility"
    assert show_toolcalls.text == "Tool-call messages shown."
    assert store.get_binding("qq", "conv-1").show_toolcalls is True


def test_router_supports_model_override_and_help_output() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)

    model = router.handle("qq", "conv-1", "/model gpt-5.4")
    assert model.action == "settings.model"
    assert model.text == "Model override set to gpt-5.4."
    assert store.get_binding("qq", "conv-1").selected_model == "gpt-5.4"

    cleared = router.handle("qq", "conv-1", "/model default")
    assert cleared.action == "settings.model"
    assert cleared.text == "Model override cleared; using the default Codex model."
    assert store.get_binding("qq", "conv-1").selected_model is None

    help_response = router.handle("qq", "conv-1", "/help")
    assert help_response.action == "help"
    assert "/cwd <path>" in help_response.text
    assert "/approve <ticket...>" in help_response.text
    assert "/permissions autonomous" in help_response.text
    assert "/model <name|default>" in help_response.text


def test_router_lists_requests_and_doctor_output() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.set_model_override("qq", "conv-1", "gpt-5.4")
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Run tests",
        payload={},
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="3",
        kind="question",
        summary="Need branch",
        payload={},
    )
    router = CommandRouter(
        store,
        diagnostics_provider=lambda: {
            "codex_bin": "codex",
            "app_server": "ws://127.0.0.1:8765",
            "bridge": "http://127.0.0.1:8000",
            "pid": 4321,
            "data_dir": ".imcodex",
        },
    )

    requests = router.handle("qq", "conv-1", "/requests")
    assert requests.action == "requests.list"
    assert "[2] approval: Run tests" in requests.text
    assert "[3] question: Need branch" in requests.text

    doctor = router.handle("qq", "conv-1", "/doctor")
    assert doctor.action == "doctor"
    assert "Codex binary: codex" in doctor.text
    assert "App Server: ws://127.0.0.1:8765" in doctor.text
    assert "PID: 4321" in doctor.text
    assert "Permission Profile: review" in doctor.text
    assert "Model: gpt-5.4" in doctor.text
    assert "Visibility: standard" in doctor.text


def test_router_supports_batch_approval_with_partial_unknown_ticket() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="approval",
        summary="Run tests",
        payload={},
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Inspect logs",
        payload={},
    )

    approve = router.handle("qq", "conv-1", "/approve 1 2 9")

    assert approve.action == "approval.accept"
    assert approve.ticket_ids == ["1", "2"]
    assert approve.missing_ticket_ids == ["9"]
    assert "Recorded accept for 1, 2." in approve.text
    assert "Unknown tickets: 9." in approve.text


def test_router_rejects_wrong_ticket_kind_for_approval_and_answer() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    router = CommandRouter(store)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="1",
        kind="question",
        summary="Need branch",
        payload={},
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Run tests",
        payload={},
    )

    approve = router.handle("qq", "conv-1", "/approve 1")
    answer = router.handle("qq", "conv-1", "/answer 2 branch=main")

    assert approve.action == "approval.accept.missing"
    assert "Unknown tickets: 1." in approve.text
    assert answer.action == "request.answer.invalid_kind"
    assert answer.text == "Ticket 2 is not a question request."


def test_status_tolerates_missing_active_thread_record() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.ensure_project(r"D:\work\alpha")
    binding = store.set_active_project("qq", "conv-1", project.project_id)
    store.set_model_override("qq", "conv-1", "gpt-5.4")
    binding.active_thread_id = "thr_missing"
    binding.last_seen_thread_name = "Recovered native thread"
    binding.last_seen_thread_path = r"D:\work\alpha\.codex\threads\thr_missing"
    binding.last_seen_thread_status = "awaitingUserInput"
    router = CommandRouter(store)

    status = router.handle("qq", "conv-1", "/status")

    assert "CWD: D:\\work\\alpha" in status.text
    assert "Thread: Recovered native thread" in status.text
    assert "Thread ID: thr_missing" in status.text
    assert "Thread Path: D:\\work\\alpha\\.codex\\threads\\thr_missing" in status.text
    assert "Thread Status: awaiting user input" in status.text
    assert "Model: gpt-5.4" in status.text
    assert "Permission Profile: review" in status.text


def test_status_does_not_leak_last_seen_thread_identity_when_no_active_thread_exists() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.ensure_project(r"D:\work\alpha")
    binding = store.set_active_project("qq", "conv-1", project.project_id)
    binding.last_seen_thread_name = "Recovered native thread"
    binding.last_seen_thread_path = r"D:\work\alpha\.codex\threads\thr_missing"
    binding.last_seen_thread_status = "awaitingUserInput"
    router = CommandRouter(store)

    status = router.handle("qq", "conv-1", "/status")

    assert "Thread: (none)" in status.text
    assert "Thread ID: (none)" in status.text
    assert "Thread Path: (none)" in status.text
    assert "Thread Status: (none)" in status.text


def test_thread_read_falls_back_to_last_seen_native_identity_when_thread_cache_is_missing() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    project = store.ensure_project(r"D:\work\alpha")
    binding = store.set_active_project("qq", "conv-1", project.project_id)
    binding.active_thread_id = "thr_missing"
    binding.last_seen_thread_name = "Recovered native thread"
    binding.last_seen_thread_path = r"D:\work\alpha\.codex\threads\thr_missing"
    binding.last_seen_thread_status = "awaitingUserInput"
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread read")

    assert response.action == "thread.read.query"
    assert response.thread_id == "thr_missing"


def test_router_project_aliases_work_after_reloading_cwd_first_state() -> None:
    state_path = _state_path("commands-cwd-first")
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="alpha")
    store.record_thread("thr_2", cwd=r"D:\work\beta", preview="beta")
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")

    try:
        reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)
        router = CommandRouter(reloaded)

        projects = router.handle("qq", "conv-1", "/projects")
        beta_project = next(project for project in reloaded.list_projects() if project.cwd == r"D:\work\beta")

        assert projects.action == "projects.list"
        assert r"D:\work\alpha" in projects.text
        assert r"D:\work\beta" in projects.text
        assert beta_project.project_id in projects.text

        response = router.handle("qq", "conv-1", f"/project use {beta_project.project_id}")

        assert response.action == "project.use"
        assert response.project_id == beta_project.project_id
        assert reloaded.get_binding("qq", "conv-1").selected_cwd == r"D:\work\beta"
    finally:
        state_path.unlink(missing_ok=True)

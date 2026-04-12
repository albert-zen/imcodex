from __future__ import annotations

from pathlib import Path

from imcodex.bridge import CommandRouter, parse_command
from imcodex.store import ConversationStore


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
    assert parse_command("/requests").name == "requests"
    assert parse_command("/doctor").name == "doctor"


def test_router_projects_and_project_switch() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    beta = store.record_thread("thr_2", cwd=r"D:\work\beta", preview="b")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/projects")
    assert response.text.startswith("Working directories:")
    assert alpha.cwd in response.text
    assert beta.cwd in response.text
    assert alpha.project_id in response.text
    assert beta.project_id in response.text

    response = router.handle("qq", "conv-1", f"/project use {beta.project_id}")
    assert response.action == "project.use"
    assert response.text.startswith("Working directory set to ")
    assert beta.cwd in response.text
    assert beta.project_id in response.text
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
    assert "seed preview" in threads.text
    assert "please inspect why the Windows working directory resets..." in threads.text
    assert threads.text.index("seed preview") < threads.text.index("thr_1")
    assert threads.text.index("please inspect why the Windows working directory resets...") < threads.text.index("thr_2")

    response = router.handle("qq", "conv-1", "/thread use thr_2")
    assert response.action == "thread.use"
    assert "please inspect why the Windows working directory resets..." in response.text
    assert response.text.index("please inspect why the Windows working directory resets...") < response.text.index("thr_2")
    assert store.get_binding("qq", "conv-1").active_thread_id == "thr_2"

    status = router.handle("qq", "conv-1", "/status")
    lines = status.text.splitlines()
    assert lines[0] == f"Working directory: {alpha.cwd}"
    assert "Thread: please inspect why the Windows working directory resets..." in status.text
    assert "Thread id: thr_2" in status.text
    assert "Permission mode: review" in status.text
    assert "Visibility profile: standard" in status.text
    assert "Commentary: shown" in status.text
    assert "Tool calls: hidden" in status.text


def test_router_thread_attach_uses_selected_working_directory() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed preview")
    router = CommandRouter(store)
    store.set_active_project("qq", "conv-1", alpha.project_id)

    response = router.handle("qq", "conv-1", "/thread attach thr_external")

    assert response.action == "thread.attach"
    assert response.thread_id == "thr_external"
    assert alpha.cwd in response.text


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


def test_router_lists_requests_and_doctor_output() -> None:
    store = ConversationStore(clock=lambda: 100.0)
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
    assert "Permission mode: review" in doctor.text
    assert "Visibility profile: standard" in doctor.text


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
    binding.active_thread_id = "thr_missing"
    router = CommandRouter(store)

    status = router.handle("qq", "conv-1", "/status")

    assert "Working directory: D:\\work\\alpha" in status.text
    assert "Thread: Untitled thread" in status.text
    assert "Thread id: thr_missing" in status.text
    assert "Permission mode: review" in status.text

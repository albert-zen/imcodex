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
    assert parse_command("/new").name == "new"
    assert parse_command("/status").name == "status"
    assert parse_command("/stop").name == "stop"


def test_parse_approval_and_answer_commands() -> None:
    assert parse_command("/approve T-1").name == "approve"
    assert parse_command("/approve-session T-2").name == "approve-session"
    assert parse_command("/deny T-3").name == "deny"
    assert parse_command("/cancel T-4").name == "cancel"
    assert parse_command("/answer T-5 timezone=Asia/Shanghai,UTC+8").name == "answer"


def test_router_projects_and_project_switch() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    beta = store.record_thread("thr_2", cwd=r"D:\work\beta", preview="b")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/projects")
    assert store.get_project(alpha.project_id).display_name in response.text
    assert store.get_project(beta.project_id).display_name in response.text

    response = router.handle("qq", "conv-1", f"/project use {beta.project_id}")
    assert response.action == "project.use"
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
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="b")
    router = CommandRouter(store)
    store.set_active_project("qq", "conv-1", alpha.project_id)

    response = router.handle("qq", "conv-1", "/thread use thr_2")
    assert response.action == "thread.use"
    assert store.get_binding("qq", "conv-1").active_thread_id == "thr_2"

    status = router.handle("qq", "conv-1", "/status")
    assert "thr_2" in status.text
    assert alpha.project_id in status.text


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
    assert approve.ticket_id == "T-9"

    answer = router.handle("qq", "conv-1", "/answer T-10 timezone=Asia/Shanghai,UTC+8")
    assert answer.action == "request.answer"
    assert answer.answers == {"timezone": ["Asia/Shanghai", "UTC+8"]}

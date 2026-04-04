from __future__ import annotations

from imcodex.store import ConversationStore


def test_thread_record_autocreates_project_from_cwd() -> None:
    store = ConversationStore(clock=lambda: 100.0)

    store.record_thread(
        thread_id="thr_1",
        cwd=r"D:\work\alpha",
        preview="hello",
    )

    projects = store.list_projects()

    assert len(projects) == 1
    assert projects[0].cwd == r"D:\work\alpha"
    assert projects[0].display_name == "alpha"
    assert store.get_thread("thr_1").project_id == projects[0].project_id


def test_record_thread_deduplicates_project_by_cwd() -> None:
    store = ConversationStore(clock=lambda: 100.0)

    first = store.record_thread(
        thread_id="thr_1",
        cwd=r"D:\work\alpha",
        preview="first",
    )
    second = store.record_thread(
        thread_id="thr_2",
        cwd=r"D:\work\alpha",
        preview="second",
    )

    assert first.project_id == second.project_id
    assert len(store.list_projects()) == 1
    assert [thread.thread_id for thread in store.list_threads(first.project_id)] == [
        "thr_1",
        "thr_2",
    ]


def test_project_and_thread_switching_updates_binding() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    beta = store.record_thread("thr_2", cwd=r"D:\work\beta", preview="b")

    binding = store.set_active_thread("qq", "conv-1", "thr_1")
    assert binding.active_project_id == alpha.project_id
    assert binding.active_thread_id == "thr_1"

    binding = store.set_active_project("qq", "conv-1", beta.project_id)
    assert binding.active_project_id == beta.project_id
    assert binding.active_thread_id is None

    binding = store.set_active_thread("qq", "conv-1", "thr_2")
    assert binding.active_project_id == beta.project_id
    assert binding.active_thread_id == "thr_2"


def test_pending_requests_round_trip() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="T-1",
        kind="approval",
        summary="Approve shell command",
        payload={"decision": "accept"},
    )

    pending = store.get_pending_request("T-1")
    assert pending is not None
    assert pending.kind == "approval"

    resolved = store.resolve_pending_request("T-1", {"decision": "accept"})
    assert resolved is not None
    assert store.get_pending_request("T-1") is None


def test_note_inbound_message_updates_binding_reply_context() -> None:
    store = ConversationStore(clock=lambda: 100.0)

    binding = store.note_inbound_message("qq", "conv-1", "msg-9")

    assert binding.last_inbound_message_id == "msg-9"
    assert store.get_binding("qq", "conv-1").last_inbound_message_id == "msg-9"

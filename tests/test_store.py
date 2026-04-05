from __future__ import annotations

from pathlib import Path

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


def test_thread_label_prefers_preview_then_first_user_message() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="Existing preview")
    store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="")

    store.note_thread_user_message(
        "thr_2",
        "please inspect why the Windows working directory resets after restart",
    )

    assert store.thread_label("thr_1") == "Existing preview"
    assert store.thread_label("thr_2") == "please inspect why the Windows working directory resets..."


def test_thread_label_persists_across_store_reload(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="")
    store.note_thread_user_message(
        "thr_1",
        "please inspect why the Windows working directory resets after restart",
    )

    reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)

    assert reloaded.thread_label("thr_1") == "please inspect why the Windows working directory resets..."


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


def test_clear_stale_active_turns_resets_old_in_progress_bindings() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("qq", "conv-1", thread.thread_id)
    store.set_active_turn(
        "qq",
        "conv-1",
        thread_id=thread.thread_id,
        turn_id="turn_old",
        status="inProgress",
    )

    cleared = store.clear_stale_active_turns()

    binding = store.get_binding("qq", "conv-1")
    assert cleared == 1
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None


def test_clear_pending_requests_for_turn_removes_only_matching_requests() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="T-1",
        kind="approval",
        summary="Approve shell command",
        payload={"decision": "accept"},
        thread_id="thr-1",
        turn_id="turn-1",
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="T-2",
        kind="approval",
        summary="Approve another shell command",
        payload={"decision": "accept"},
        thread_id="thr-1",
        turn_id="turn-2",
    )

    cleared = store.clear_pending_requests_for_turn(
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr-1",
        turn_id="turn-1",
    )

    assert cleared == 1
    assert store.get_pending_request("T-1") is None
    assert store.get_pending_request("T-2") is not None
    assert store.get_binding("qq", "conv-1").pending_request_ids == ["T-2"]

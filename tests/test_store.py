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


def test_thread_label_keeps_long_whitespace_free_prompts_readable() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="")

    prompt = "https://example.com/very/long/path/without/spaces/or/breakpoints/abcdef1234567890"
    store.note_thread_user_message("thr_1", prompt)

    label = store.thread_label("thr_1")
    assert label != "..."
    assert label.startswith("https://example.com/very/long")
    assert label.endswith("...")


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


def test_permission_and_visibility_settings_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)

    store.set_permission_profile("qq", "conv-1", "autonomous")
    store.set_visibility_profile("qq", "conv-1", "verbose")
    store.set_commentary_visibility("qq", "conv-1", enabled=False)
    store.set_toolcall_visibility("qq", "conv-1", enabled=True)

    reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)
    binding = reloaded.get_binding("qq", "conv-1")

    assert binding.permission_profile == "autonomous"
    assert binding.visibility_profile == "verbose"
    assert binding.show_commentary is False
    assert binding.show_toolcalls is True


def test_list_pending_requests_returns_binding_order() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Second",
        payload={},
    )
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="3",
        kind="question",
        summary="Third",
        payload={},
    )

    requests = store.list_pending_requests("qq", "conv-1")

    assert [request.ticket_id for request in requests] == ["2", "3"]


def test_switching_back_to_delayed_running_thread_restores_its_turn_state() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    old_thread = store.record_thread("thr_old", cwd=r"D:\work\alpha", preview="old")
    new_thread = store.record_thread("thr_new", cwd=r"D:\work\alpha", preview="new")
    store.set_active_thread("qq", "conv-1", old_thread.thread_id)
    store.set_active_thread("qq", "conv-1", new_thread.thread_id)

    store.note_turn_started(old_thread.thread_id, turn_id="turn_old", status="inProgress")

    binding = store.get_binding("qq", "conv-1")
    assert binding.active_thread_id == new_thread.thread_id
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None

    binding = store.set_active_thread("qq", "conv-1", old_thread.thread_id)
    assert binding.active_turn_id == "turn_old"
    assert binding.active_turn_status == "inProgress"


def test_delayed_completion_for_old_thread_does_not_overwrite_current_thread_status() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    old_thread = store.record_thread("thr_old", cwd=r"D:\work\alpha", preview="old")
    new_thread = store.record_thread("thr_new", cwd=r"D:\work\alpha", preview="new")
    store.set_active_thread("qq", "conv-1", old_thread.thread_id)
    store.note_turn_started(old_thread.thread_id, turn_id="turn_old", status="inProgress")
    store.set_active_thread("qq", "conv-1", new_thread.thread_id)
    store.note_turn_started(new_thread.thread_id, turn_id="turn_new", status="inProgress")

    store.note_turn_completed(old_thread.thread_id, turn_id="turn_old", status="completed")

    binding = store.get_binding("qq", "conv-1")
    assert binding.active_thread_id == new_thread.thread_id
    assert binding.active_turn_id == "turn_new"
    assert binding.active_turn_status == "inProgress"

    binding = store.set_active_thread("qq", "conv-1", old_thread.thread_id)
    assert binding.active_turn_id is None
    assert binding.active_turn_status is None

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from imcodex.store import ConversationStore


def _state_path(name: str) -> Path:
    return Path.cwd() / f".pytest-state-{name}-{uuid4().hex}.json"


def test_record_thread_tracks_cwd_and_list_threads() -> None:
    store = ConversationStore(clock=lambda: 100.0)

    first = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="first")
    second = store.record_thread("thr_2", cwd=r"D:\work\alpha", preview="second")

    assert first.cwd == r"D:\work\alpha"
    assert second.cwd == r"D:\work\alpha"
    assert [thread.thread_id for thread in store.list_threads()] == ["thr_1", "thr_2"]
    assert [thread.thread_id for thread in store.list_threads_for_cwd(r"D:\work\alpha")] == ["thr_1", "thr_2"]


def test_cwd_and_thread_switching_updates_binding() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    alpha = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    beta = store.record_thread("thr_2", cwd=r"D:\work\beta", preview="b")

    binding = store.set_active_thread("qq", "conv-1", alpha.thread_id)
    assert binding.selected_cwd == alpha.cwd
    assert binding.active_thread_id == alpha.thread_id

    binding = store.set_selected_cwd("qq", "conv-1", beta.cwd)
    assert binding.selected_cwd == beta.cwd
    assert binding.active_thread_id is None

    binding = store.set_active_thread("qq", "conv-1", beta.thread_id)
    assert binding.selected_cwd == beta.cwd
    assert binding.active_thread_id == beta.thread_id


def test_set_selected_cwd_updates_binding_and_clears_active_thread() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="a")
    store.set_active_thread("qq", "conv-1", thread.thread_id)

    binding = store.set_selected_cwd("qq", "conv-1", r"D:\work\beta")

    assert binding.selected_cwd == r"D:\work\beta"
    assert binding.active_thread_id is None


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


def test_thread_label_falls_back_when_imported_name_clips_to_empty() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.record_thread(
        "thr_1",
        cwd=r"D:\work\alpha",
        preview="Existing preview",
        name="   ",
    )

    assert store.thread_label("thr_1") == "Existing preview"


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


def test_note_thread_status_updates_active_binding_snapshot() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("qq", "conv-1", thread.thread_id)

    store.note_thread_status("thr_1", status="stale")

    assert store.get_thread("thr_1").status == "stale"
    assert store.get_binding("qq", "conv-1").last_seen_thread_status == "stale"


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
    store.set_model_override("qq", "conv-1", "gpt-5.4")
    store.set_visibility_profile("qq", "conv-1", "verbose")
    store.set_commentary_visibility("qq", "conv-1", enabled=False)
    store.set_toolcall_visibility("qq", "conv-1", enabled=True)

    reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)
    binding = reloaded.get_binding("qq", "conv-1")

    assert binding.permission_profile == "autonomous"
    assert binding.selected_model == "gpt-5.4"
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


def test_can_find_pending_request_by_native_request_id() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    store.create_pending_request(
        channel_id="qq",
        conversation_id="conv-1",
        ticket_id="2",
        kind="approval",
        summary="Second",
        payload={},
        request_id="native-22",
    )

    pending = store.get_pending_request_by_request_id("native-22")

    assert pending is not None
    assert pending.ticket_id == "2"


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


def test_state_persists_cwd_first_without_project_aliases() -> None:
    state_path = _state_path("store-cwd-first")
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)
    store.set_selected_cwd("qq", "conv-1", r"D:\work\alpha")
    store.record_thread("thr_alpha", cwd=r"D:\work\alpha", preview="alpha")
    store.record_thread("thr_beta", cwd=r"D:\work\beta", preview="beta")

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))

        assert "projects" not in payload
        assert "project_aliases" not in payload
        assert all("project_id" not in thread for thread in payload["threads"])
        assert all("active_project_id" not in binding for binding in payload["bindings"])

        reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)
        assert [thread.thread_id for thread in reloaded.list_threads_for_cwd(r"D:\work\alpha")] == ["thr_alpha"]
        assert [thread.thread_id for thread in reloaded.list_threads_for_cwd(r"D:\work\beta")] == ["thr_beta"]
        assert reloaded.get_binding("qq", "conv-1").selected_cwd == r"D:\work\alpha"
    finally:
        state_path.unlink(missing_ok=True)


def test_legacy_project_fields_are_ignored_on_reload() -> None:
    state_path = _state_path("store-legacy-project-id")
    payload = {
        "projects": [
            {
                "project_id": "proj_alpha",
                "cwd": r"D:\work\alpha",
                "display_name": "alpha",
                "last_used_at": 42.0,
                "created_seq": 1,
            }
        ],
        "threads": [
            {
                "thread_id": "thr_alpha",
                "preview": "alpha",
                "status": "idle",
                "last_used_at": 42.0,
                "cwd": r"D:\work\alpha",
                "project_id": "proj_alpha",
                "name": None,
                "path": None,
                "last_turn_id": None,
                "last_turn_status": None,
                "stale_turn_ids": [],
                "created_seq": 1,
            }
        ],
        "bindings": [
            {
                "channel_id": "qq",
                "conversation_id": "conv-1",
                "active_project_id": "proj_alpha",
                "selected_cwd": r"D:\work\alpha",
                "selected_model": None,
                "active_thread_id": "thr_alpha",
                "active_turn_id": None,
                "active_turn_status": None,
                "last_inbound_message_id": None,
                "pending_request_ids": [],
                "next_ticket": 1,
                "known_thread_ids": ["thr_alpha"],
                "permission_profile": "review",
                "visibility_profile": "standard",
                "show_commentary": True,
                "show_toolcalls": False,
                "last_seen_thread_name": None,
                "last_seen_thread_path": None,
                "last_seen_thread_status": None,
            }
        ],
        "pending_requests": [],
        "thread_active_turns": [],
        "thread_first_user_messages": {},
        "pending_first_thread_labels": [],
        "thread_order": ["thr_alpha"],
        "seq": 1,
    }
    state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    try:
        reloaded = ConversationStore(clock=lambda: 200.0, state_path=state_path)

        assert [thread.thread_id for thread in reloaded.list_threads()] == ["thr_alpha"]
        assert reloaded.get_binding("qq", "conv-1").selected_cwd == r"D:\work\alpha"
        assert not hasattr(reloaded.get_thread("thr_alpha"), "project_id")
        assert not hasattr(reloaded.get_binding("qq", "conv-1"), "active_project_id")
    finally:
        state_path.unlink(missing_ok=True)

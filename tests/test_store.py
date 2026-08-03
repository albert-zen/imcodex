from __future__ import annotations

import json

import pytest

from imcodex.store import ConversationStore


@pytest.mark.asyncio
async def test_store_persists_only_minimal_native_first_state(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.set_bootstrap_cwd("qq", "conv-1", "/work/alpha")
    store.bind_thread("qq", "conv-1", "thr-1")
    await store.flush_pending_writes()

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["version"] == 2
    assert payload["bindings"][0]["thread_id"] == "thr-1"
    assert payload["pending_terminal_deliveries"] == []
    assert "terminal_delivery_watches" not in payload
    assert "acknowledged_terminal_deliveries" not in payload
    assert "pending_requests" not in payload
    assert not hasattr(store, "note_active_turn")
    assert not hasattr(store, "stage_terminal_delivery")


@pytest.mark.asyncio
async def test_store_reads_and_only_consumes_legacy_delivery_evidence(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": [],
                "pending_terminal_deliveries": [
                    {
                        "thread_id": "thr-1",
                        "turn_id": "turn-watch",
                        "message": None,
                        "created_at": 1.0,
                    },
                    {
                        "delivery_id": "legacy-1",
                        "thread_id": "thr-1",
                        "turn_id": "turn-1",
                        "message": {
                            "channel_id": "qq",
                            "conversation_id": "conv-1",
                            "message_type": "result",
                            "text": "owed",
                            "metadata": {"delivery_id": "legacy-1"},
                            "artifacts": [{"local_path": "/spool/result.txt"}],
                        },
                        "created_at": 2.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ConversationStore(clock=lambda: 3.0, state_path=state_path)

    pending = store.list_legacy_delivery_evidence()

    assert [(item.delivery_id, item.turn_id) for item in pending] == [
        ("legacy-1", "turn-1")
    ]
    assert store.referenced_legacy_artifact_paths() == {"/spool/result.txt"}
    assert not hasattr(store, "stage_terminal_delivery")
    assert not hasattr(store, "update_terminal_delivery_message")

    await store.consume_legacy_delivery_evidence("legacy-1")

    assert ConversationStore(
        clock=lambda: 4.0,
        state_path=state_path,
    ).list_legacy_delivery_evidence() == []


@pytest.mark.asyncio
async def test_legacy_evidence_consume_restores_memory_when_persistence_fails(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": [],
                "pending_terminal_deliveries": [
                    {
                        "delivery_id": "legacy-1",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "message": {
                            "channel_id": "qq",
                            "conversation_id": "conv-1",
                            "message_type": "result",
                            "text": "owed",
                            "metadata": {"delivery_id": "legacy-1"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)

    async def fail_write(_serialized, _revision) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_state_async", fail_write)

    with pytest.raises(RuntimeError, match="Could not persist bridge state"):
        await store.consume_legacy_delivery_evidence("legacy-1")

    assert [
        item.delivery_id for item in store.list_legacy_delivery_evidence()
    ] == ["legacy-1"]
    assert json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_terminal_deliveries"
    ]


def test_store_fails_explicitly_on_legacy_or_corrupt_state(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"bindings":[{"channel_id":"qq"}]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unsupported or invalid bridge state"):
        ConversationStore(clock=lambda: 1.0, state_path=state_path)

    state_path.write_text("{truncated", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Could not load bridge state"):
        ConversationStore(clock=lambda: 1.0, state_path=state_path)


def test_store_atomic_save_preserves_previous_state_on_replace_failure(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.set_bootstrap_cwd("qq", "conv-1", "/first")
    previous = state_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "imcodex.store.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(OSError, match="disk failure"):
        store.set_bootstrap_cwd("qq", "conv-1", "/second")

    assert state_path.read_text(encoding="utf-8") == previous


def test_multiple_conversations_can_bind_the_same_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "old", "thr-1")
    store.bind_thread("qq", "new", "thr-1")

    assert store.get_binding("qq", "old").thread_id == "thr-1"
    assert store.get_binding("qq", "new").thread_id == "thr-1"


def test_conversation_switch_persists_only_its_current_thread(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.bind_thread("qq", "conv-1", "thr-a")
    store.bind_thread("qq", "conv-1", "thr-b")

    reloaded = ConversationStore(clock=lambda: 2.0, state_path=state_path)

    assert reloaded.get_binding("qq", "conv-1").thread_id == "thr-b"
    assert "thread_recipient_routes" not in state_path.read_text(encoding="utf-8")


def test_sdk_command_context_projection_does_not_clear_another_subscriber() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread_with_cwd("qq", "selected", "thr-a", "/selected")

    store.project_sdk_thread_context("qq", "rebuild", "thr-a", "/authoritative")

    assert store.get_binding("qq", "selected").thread_id == "thr-a"
    rebuilt = store.get_binding("qq", "rebuild")
    assert (rebuilt.thread_id, rebuilt.bootstrap_cwd) == (
        "thr-a",
        "/authoritative",
    )


def test_visibility_preferences_persist_without_thread_or_cwd(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    store.set_visibility_profile("qq", "conv-1", "verbose")

    binding = ConversationStore(clock=lambda: 2.0, state_path=state_path).get_binding(
        "qq", "conv-1"
    )

    assert binding.visibility_profile == "verbose"
    assert binding.show_commentary is True
    assert binding.show_toolcalls is True
    assert binding.show_system is True


def test_thread_browser_context_is_runtime_only_and_expires(tmp_path) -> None:
    now = [1.0]
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: now[0], state_path=state_path)
    store.set_thread_browser_context(
        "qq",
        "conv-1",
        thread_ids=["thr-1"],
        page=1,
        total=1,
        query=None,
        ttl_s=10.0,
    )
    assert store.get_thread_browser_context("qq", "conv-1") is not None

    now[0] = 12.0
    assert store.get_thread_browser_context("qq", "conv-1") is None
    assert (
        ConversationStore(clock=lambda: 2.0, state_path=state_path)
        .get_thread_browser_context("qq", "conv-1")
        is None
    )


def test_store_has_no_bridge_owned_next_model_override_state() -> None:
    store = ConversationStore(clock=lambda: 1.0)

    assert not hasattr(store, "set_next_model")
    assert not hasattr(store, "pop_next_model")

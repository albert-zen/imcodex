from __future__ import annotations

from pathlib import Path

from imcodex.bridge import CommandRouter
from imcodex.store import ConversationStore


def test_requests_command_is_no_longer_supported() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/requests")

    assert response.action == "unknown"
    assert "Unknown command" in response.text


def test_approve_without_id_targets_single_pending_request() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/approve")

    assert response.action == "approval.accept"
    assert response.request_ids == ["native-request-abcdef"]


def test_approve_without_id_targets_all_pending_approvals() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    for index, suffix in enumerate(("abc", "def"), start=1):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=90 + index,
            connection_epoch=1,
        )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/approve")

    assert response.action == "approval.accept"
    assert response.request_ids == ["native-request-abc", "native-request-def"]


def test_approve_prefix_must_be_unique() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    for suffix in ("111", "222"):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=suffix,
            connection_epoch=1,
        )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/approve native-request-")

    assert response.action == "approval.accept.missing"
    assert "Ambiguous" in response.text


def test_help_lists_compact_top_level_commands_with_examples() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store, playground_path=Path(r"D:\Playground"))

    response = router.handle("qq", "conv-1", "/help")

    assert response.action == "help"
    assert "/cwd <path>" in response.text
    assert "/cwd playground" in response.text
    assert "/threads" in response.text
    assert "/pick <n>" in response.text
    assert "/model [model-id]" in response.text
    assert "/model gpt-5.4" in response.text
    assert "/permission [mode]" in response.text
    assert "/permission full-access" in response.text
    assert "/thread attach" not in response.text
    assert "/approve" not in response.text
    assert "Doctor" not in response.text


def test_cwd_without_args_reads_current_path() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/cwd")

    assert response.action == "project.cwd.read"
    assert "D:\\work\\alpha" in response.text


def test_cwd_playground_uses_configured_default_folder(tmp_path) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    playground = tmp_path / "Codex Playground"
    router = CommandRouter(store, playground_path=playground)

    response = router.handle("qq", "conv-1", "/cwd playground")

    assert response.action == "project.cwd"
    assert playground.is_dir()
    assert store.current_cwd("qq", "conv-1") == str(playground)


def test_threads_command_accepts_query_and_page_flags() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/threads polish --page 2")

    assert response.action == "threads.query"
    assert response.payload == {"page": 2, "query": "polish"}


def test_next_requires_active_thread_browser_context() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/next")

    assert response.action == "threads.browser.missing"
    assert response.text == "Use /threads first."


def test_pick_uses_current_thread_browser_page() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_thread_browser_context(
        "qq",
        "conv-1",
        thread_ids=["thr_1", "thr_2"],
        page=1,
        total=2,
        query=None,
        include_all=False,
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/pick 2")

    assert response.action == "thread.pick"
    assert response.payload == {"index": 1}


def test_thread_attach_accepts_human_readable_selector() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread attach repo polish")

    assert response.action == "thread.attach"
    assert response.payload == {"selector": "repo polish"}


def test_model_without_args_opens_browser() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/model")

    assert response.action == "models.list"


def test_permission_without_args_opens_browser() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/permission")

    assert response.action == "settings.permission.read"


def test_permission_with_mode_builds_native_permission_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/permission full-access")

    assert response.action == "settings.permission.write"
    assert response.payload == {
        "mode": "full-access",
        "edits": [
            {"key_path": "approval_policy", "value": "never", "merge_strategy": "replace"},
            {"key_path": "sandbox_mode", "value": "danger-full-access", "merge_strategy": "replace"},
        ],
    }


def test_show_system_updates_bridge_visibility_only() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/show system")

    assert response.action == "settings.visibility"
    assert store.get_binding("qq", "conv-1").show_system is True


def test_native_help_exposes_advanced_escape_hatch_commands() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/native help")

    assert response.action == "native.help"
    assert "/native call <method> <json>" in response.text
    assert "/native requests" not in response.text

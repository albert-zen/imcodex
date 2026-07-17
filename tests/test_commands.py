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
    assert response.text.startswith("Help\n\nStart")
    assert "/cwd <path>" in response.text
    assert "/threads [query]" in response.text
    assert "/history [turns]" in response.text
    assert "/fork" in response.text
    assert "/rename <name>" in response.text
    assert "/compact" in response.text
    assert "/goal [objective|pause|resume|clear]" in response.text
    assert "/credits" in response.text
    assert "/model [model-id]" in response.text
    assert "/think [effort]" in response.text
    assert "/personality [style]" in response.text
    assert "/fast [on|off|status]" in response.text
    assert "native reasoning effort" not in response.text
    assert "native Codex Fast mode" not in response.text
    assert "/permission [mode]" in response.text
    assert "/thread attach" not in response.text
    assert "/approve" not in response.text
    assert "/native help" in response.text
    assert "/native call" not in response.text
    assert "currentTime/read" not in response.text
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


def test_threads_command_rejects_unknown_flags() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/threads --all")

    assert response.action == "threads.invalid"
    assert response.text == "Usage: /threads [query] [--page N]"


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
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/pick 2")

    assert response.action == "thread.pick"
    assert response.payload == {"index": 1}


def test_pick_accepts_optional_history_limit() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_thread_browser_context(
        "qq",
        "conv-1",
        thread_ids=["thr_1", "thr_2"],
        page=1,
        total=2,
        query=None,
    )
    router = CommandRouter(store)

    default_history = router.handle("qq", "conv-1", "/pick 2 --history")
    explicit_history = router.handle("qq", "conv-1", "/pick 2 --history 3")
    equals_history = router.handle("qq", "conv-1", "/pick 2 --history=4")

    assert default_history.payload == {"index": 1, "history_limit": 1}
    assert explicit_history.payload == {"index": 1, "history_limit": 3}
    assert equals_history.payload == {"index": 1, "history_limit": 4}


def test_pick_rejects_invalid_history_limit() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_thread_browser_context(
        "qq",
        "conv-1",
        thread_ids=["thr_1"],
        page=1,
        total=1,
        query=None,
    )
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/pick 1 --history 6")

    assert response.action == "thread.pick.invalid"
    assert response.text == "History turns must be between 1 and 5."


def test_thread_attach_accepts_human_readable_selector() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread attach repo polish")

    assert response.action == "thread.attach"
    assert response.payload == {"selector": "repo polish"}


def test_thread_history_requires_active_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread history")

    assert response.action == "thread.history.missing"
    assert response.text == "No active thread."


def test_thread_history_reads_active_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/thread history")

    assert response.action == "thread.history.query"
    assert response.thread_id == "thr_1"
    assert response.payload == {"limit": 1}


def test_history_is_primary_command_and_accepts_bounded_limit() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/history 3")

    assert response.action == "thread.history.query"
    assert response.thread_id == "thr_1"
    assert response.payload == {"limit": 3}


def test_native_thread_operations_require_active_thread() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    assert router.handle("qq", "conv-1", "/fork").action == "thread.fork.missing"
    assert router.handle("qq", "conv-1", "/rename Ship notes").action == "thread.rename.missing"
    assert router.handle("qq", "conv-1", "/compact").action == "thread.compact.missing"


def test_native_thread_operations_parse_payloads() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "conv-1", "thr_1")
    router = CommandRouter(store)

    fork = router.handle("qq", "conv-1", "/fork")
    rename = router.handle("qq", "conv-1", "/rename Ship the thread polish")
    compact = router.handle("qq", "conv-1", "/compact")

    assert fork.action == "thread.fork"
    assert rename.action == "thread.rename"
    assert rename.payload == {"name": "Ship the thread polish"}
    assert compact.action == "thread.compact"


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


def test_think_with_effort_builds_native_reasoning_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/think xhigh")

    assert response.action == "settings.reasoning.write"
    assert response.payload == {"effort": "xhigh"}


def test_think_accepts_native_catalog_values_without_a_bridge_allowlist() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/think ultra")

    assert response.action == "settings.reasoning.write"
    assert response.payload == {"effort": "ultra"}


def test_think_default_clears_native_reasoning_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/think default")

    assert response.action == "settings.reasoning.write"
    assert response.payload == {"effort": None}


def test_personality_without_args_reads_native_config() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/personality")

    assert response.action == "settings.personality.read"


def test_personality_builds_native_config_payload_and_default_clears_it() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    pragmatic = router.handle("qq", "conv-1", "/personality pragmatic")
    default = router.handle("qq", "conv-1", "/personality default")

    assert pragmatic.action == "settings.personality.write"
    assert pragmatic.payload == {"personality": "pragmatic"}
    assert default.action == "settings.personality.write"
    assert default.payload == {"personality": None}


def test_personality_rejects_values_outside_the_native_enum() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/personality cheerful")

    assert response.action == "settings.personality.invalid"


def test_fast_on_builds_native_fast_mode_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/fast on")

    assert response.action == "settings.fast.write"
    assert response.payload == {"enabled": True}


def test_fast_off_builds_native_standard_tier_intent() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/fast off")

    assert response.action == "settings.fast.write"
    assert response.payload == {"enabled": False}


def test_fast_status_reads_native_config() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/fast status")

    assert response.action == "settings.fast.read"


def test_credits_command_reads_account_rate_limits() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/credits")

    assert response.action == "credits.read"


def test_goal_without_args_reads_native_goal() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/goal")

    assert response.action == "goal.read"


def test_goal_with_objective_builds_native_goal_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/goal Finish the migration and keep tests green")

    assert response.action == "goal.set"
    assert response.payload == {"objective": "Finish the migration and keep tests green"}


def test_goal_subcommands_map_to_native_status_or_clear() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    pause = router.handle("qq", "conv-1", "/goal pause")
    resume = router.handle("qq", "conv-1", "/goal resume")
    clear = router.handle("qq", "conv-1", "/goal clear")

    assert pause.action == "goal.status"
    assert pause.payload == {"status": "paused"}
    assert resume.action == "goal.status"
    assert resume.payload == {"status": "active"}
    assert clear.action == "goal.clear"


def test_goal_rejects_oversized_objective_before_native_call() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/goal " + "x" * 4001)

    assert response.action == "goal.invalid"
    assert "4000" in response.text


def test_permission_with_mode_builds_native_permission_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/permission full-access")

    assert response.action == "settings.permission.write"
    assert response.payload == {"mode": "full-access"}


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


def test_native_events_command_builds_filter_payload() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/native events outcome=rejected method=item/tool --limit 3")

    assert response.action == "native.events"
    assert response.payload == {"filters": ["outcome=rejected", "method=item/tool"], "limit": 3}


def test_native_events_command_rejects_invalid_limit() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    router = CommandRouter(store)

    response = router.handle("qq", "conv-1", "/native events --limit 0")

    assert response.action == "native.events.invalid"
    assert "at least 1" in response.text

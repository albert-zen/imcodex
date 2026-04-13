from __future__ import annotations

from imcodex.bridge.session_registry import SessionRegistry
from imcodex.bridge.visibility import VisibilityClassifier
from imcodex.store import ConversationStore


def test_visibility_classifier_hides_commentary_when_binding_disables_it() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)

    classifier = VisibilityClassifier()

    assert classifier.should_emit("commentary", thread_id="thr_1", store=store) is False


def test_visibility_classifier_hides_toolcalls_by_default_and_can_show_them() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    classifier = VisibilityClassifier()

    assert classifier.should_emit("toolcall", thread_id="thr_1", store=store) is False

    store.set_toolcall_visibility("demo", "conv-1", enabled=True)

    assert classifier.should_emit("toolcall", thread_id="thr_1", store=store) is True


def test_visibility_classifier_always_shows_final_output() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    store.set_active_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    store.set_toolcall_visibility("demo", "conv-1", enabled=False)

    classifier = VisibilityClassifier()

    assert classifier.should_emit("final", thread_id="thr_1", store=store) is True


def test_visibility_classifier_prefers_runtime_session_index_over_store_scan() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    registry = SessionRegistry(store)
    registry.bind_cwd("demo", "conv-1", thread.cwd)
    registry.bind_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    registry.bind_cwd("demo", "conv-2", thread.cwd)
    registry.bind_thread("demo", "conv-2", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-2", enabled=True)

    classifier = VisibilityClassifier(session_registry=registry)

    assert classifier.should_emit("commentary", thread_id="thr_1", store=store) is True


def test_visibility_classifier_drops_detached_thread_even_if_store_keeps_historical_binding() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    thread = store.record_thread("thr_1", cwd=r"D:\work\alpha", preview="seed")
    registry = SessionRegistry(store)
    registry.bind_cwd("demo", "conv-1", thread.cwd)
    registry.bind_thread("demo", "conv-1", thread.thread_id)
    store.set_commentary_visibility("demo", "conv-1", enabled=False)
    store.set_selected_cwd("demo", "conv-1", r"D:\work\beta")
    registry.sync("demo", "conv-1")

    classifier = VisibilityClassifier(session_registry=registry)

    assert classifier.should_emit("commentary", thread_id="thr_1", store=store) is True
    assert classifier.should_emit("toolcall", thread_id="thr_1", store=store) is False

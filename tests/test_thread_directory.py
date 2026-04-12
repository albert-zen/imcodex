from __future__ import annotations

import ntpath
from pathlib import Path

from imcodex.bridge.thread_directory import ThreadDirectory
from imcodex.store import ConversationStore


def test_thread_directory_imports_native_thread_metadata_into_store(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ConversationStore(clock=lambda: 100.0, state_path=state_path)
    directory = ThreadDirectory(store)

    directory.import_threads(
        [
            {
                "id": "thr_1",
                "name": "Investigate alpha",
                "path": r"D:\work\alpha",
                "cwd": r"D:\work\alpha",
                "status": "idle",
                "preview": "Check the failing tests",
            },
            {
                "id": "thr_2",
                "name": "Fix beta",
                "path": r"D:\work\beta",
                "cwd": r"D:\work\beta",
                "status": "completed",
                "preview": "Done",
            },
        ]
    )

    snapshots = directory.list_threads()
    assert [snapshot.thread_id for snapshot in snapshots] == ["thr_1", "thr_2"]
    assert snapshots[0].name == "Investigate alpha"
    assert snapshots[0].cwd == r"D:\work\alpha"
    assert store.get_thread("thr_1").name == "Investigate alpha"

    reloaded = ThreadDirectory(ConversationStore(clock=lambda: 200.0, state_path=state_path))
    snapshot = reloaded.get("thr_1")
    assert snapshot is not None
    assert snapshot.path == r"D:\work\alpha"
    assert snapshot.preview == "Check the failing tests"


def test_thread_directory_can_filter_by_cwd() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    directory = ThreadDirectory(store)

    directory.remember_thread(
        thread_id="thr_1",
        cwd=r"D:\work\alpha",
        preview="Alpha",
        name="Alpha thread",
        path=r"D:\work\alpha",
        status="idle",
    )
    directory.remember_thread(
        thread_id="thr_2",
        cwd=r"D:\work\beta",
        preview="Beta",
        name="Beta thread",
        path=r"D:\work\beta",
        status="idle",
    )

    alpha_threads = directory.list_threads(cwd=r"D:\work\alpha")

    assert [snapshot.thread_id for snapshot in alpha_threads] == ["thr_1"]


def test_thread_directory_normalizes_windows_cwd_variants_when_filtering() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    directory = ThreadDirectory(store)

    directory.remember_thread(
        thread_id="thr_1",
        cwd=r"D:\work\alpha",
        preview="Alpha",
        name="Alpha thread",
        path=r"D:\work\alpha",
        status="idle",
    )

    snapshots = directory.list_threads(cwd="d:/WORK/alpha")

    assert [snapshot.thread_id for snapshot in snapshots] == ["thr_1"]


def test_thread_directory_preserves_case_for_posix_style_filters() -> None:
    store = ConversationStore(clock=lambda: 100.0)
    directory = ThreadDirectory(store)

    directory.remember_thread(
        thread_id="thr_upper",
        cwd="/Work/Alpha",
        preview="Upper",
        name="Upper thread",
        path="/Work/Alpha",
        status="idle",
    )
    directory.remember_thread(
        thread_id="thr_lower",
        cwd="/work/alpha",
        preview="Lower",
        name="Lower thread",
        path="/work/alpha",
        status="idle",
    )

    snapshots = directory.list_threads(cwd="/work/alpha")

    assert [snapshot.thread_id for snapshot in snapshots] == ["thr_lower"]


def test_windows_path_normalization_is_host_independent() -> None:
    assert ThreadDirectory._normalize_cwd(r"D:\work\alpha") == ntpath.normcase(
        ntpath.normpath("d:/WORK/alpha")
    )


def test_posix_path_normalization_is_host_independent() -> None:
    assert ThreadDirectory._normalize_cwd("/Work/Alpha") != ThreadDirectory._normalize_cwd(
        "/work/alpha"
    )

from __future__ import annotations

from pathlib import Path

from imcodex.bridge.thread_views import ThreadViewMixin
from imcodex.models import NativeThreadSnapshot


def _snapshot(thread_id: str, cwd: str) -> NativeThreadSnapshot:
    return NativeThreadSnapshot(
        thread_id=thread_id,
        cwd=cwd,
        preview=thread_id,
        status="idle",
    )


def test_thread_project_paths_collapse_linked_worktree_to_common_repo(
    tmp_path: Path,
) -> None:
    main_repo = tmp_path / "main" / "imcodex"
    common_dir = main_repo / ".git"
    worktree_git_dir = common_dir / "worktrees" / "feature"
    worktree_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktrees" / "feature" / "imcodex"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git_dir}\n",
        encoding="utf-8",
    )
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    snapshot = _snapshot("thr_worktree", str(worktree))

    project_paths = ThreadViewMixin()._thread_project_paths([snapshot])

    assert project_paths == {"thr_worktree": str(main_repo)}
    assert snapshot.cwd == str(worktree)


def test_linked_worktree_and_main_checkout_share_one_project_filter(
    tmp_path: Path,
) -> None:
    main_repo = tmp_path / "main" / "imcodex"
    common_dir = main_repo / ".git"
    worktree_git_dir = common_dir / "worktrees" / "feature"
    worktree_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktrees" / "feature" / "imcodex"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git_dir}\n",
        encoding="utf-8",
    )
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    threads = [
        _snapshot("thr_main", str(main_repo)),
        _snapshot("thr_worktree", str(worktree)),
    ]
    view = ThreadViewMixin()

    project_paths = view._thread_project_paths(threads)
    options = view._thread_project_options(threads, project_paths=project_paths)
    filtered = view._filter_threads_by_project(
        threads,
        str(main_repo),
        project_paths=project_paths,
    )

    assert options == [(str(main_repo), "imcodex")]
    assert [snapshot.thread_id for snapshot in filtered] == ["thr_main", "thr_worktree"]


def test_thread_project_options_disambiguate_same_named_projects(
    tmp_path: Path,
) -> None:
    first = tmp_path / "team-one" / "api"
    second = tmp_path / "team-two" / "api"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    view = ThreadViewMixin()
    snapshots = [_snapshot("thr_one", str(first)), _snapshot("thr_two", str(second))]

    options = view._thread_project_options(
        snapshots,
        project_paths={"thr_one": str(first), "thr_two": str(second)},
    )

    assert options == [
        (str(first), "team-one/api"),
        (str(second), "team-two/api"),
    ]


def test_thread_project_path_keeps_non_git_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "notes"
    directory.mkdir()
    snapshot = _snapshot("thr_notes", str(directory))
    monkeypatch.setattr(ThreadViewMixin, "_git_project_root", lambda _self, _path: None)

    assert ThreadViewMixin()._thread_project_path(snapshot) == str(directory)


def test_thread_project_paths_do_not_split_catalog_after_two_hundred(
    monkeypatch,
) -> None:
    threads = [_snapshot(f"thr_{index}", f"/worktrees/{index}") for index in range(205)]
    calls: list[str] = []

    def project_root(_self, path: str) -> str:
        calls.append(path)
        return "/repos/shared"

    monkeypatch.setattr(ThreadViewMixin, "_git_project_root", project_root)
    view = ThreadViewMixin()

    project_paths = view._thread_project_paths(threads)
    options = view._thread_project_options(threads, project_paths=project_paths)

    assert len(calls) == 205
    assert options == [("/repos/shared", "shared")]


def test_thread_browser_title_collapses_lines_and_bounds_long_preview() -> None:
    snapshot = NativeThreadSnapshot(
        thread_id="thr_long",
        cwd="/work/imcodex",
        preview="First line\n  second line " + ("detail " * 30),
        status="idle",
    )

    title = ThreadViewMixin()._thread_browser_title(snapshot)

    assert "\n" not in title
    assert "  " not in title
    assert len(title) == 72
    assert title.endswith("…")


def test_thread_identity_uses_native_status_and_relative_age() -> None:
    snapshot = NativeThreadSnapshot(
        thread_id="thr_active",
        cwd="/work/imcodex",
        preview="Active thread",
        status="inProgress",
        updated_at=9_000,
    )
    view = ThreadViewMixin()

    identity = view._thread_identity_label(
        snapshot,
        project_options=[("/work/imcodex", "imcodex")],
        project_path="/work/imcodex",
    )

    assert identity.startswith("imcodex · Working")
    assert view._thread_age_label(snapshot.updated_at, now=10_000) == "16m ago"
    assert view._thread_status_label("systemError") == "Error"
    assert view._thread_status_label("notLoaded") is None

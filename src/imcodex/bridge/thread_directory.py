from __future__ import annotations

from dataclasses import dataclass
import ntpath
import posixpath

from ..store import ConversationStore


@dataclass(slots=True)
class NativeThreadSnapshot:
    thread_id: str
    cwd: str
    preview: str
    status: str
    name: str | None
    path: str | None


class ThreadDirectory:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store

    def remember_thread(
        self,
        *,
        thread_id: str,
        cwd: str,
        preview: str,
        status: str,
        name: str | None = None,
        path: str | None = None,
    ) -> NativeThreadSnapshot:
        thread = self.store.record_thread(
            thread_id=thread_id,
            cwd=cwd,
            preview=preview,
            status=str(status or "idle"),
            name=name,
            path=path,
        )
        return self._to_snapshot(thread)

    def import_threads(self, items: list[dict]) -> list[NativeThreadSnapshot]:
        snapshots: list[NativeThreadSnapshot] = []
        for item in items:
            snapshots.append(
                self.remember_thread(
                    thread_id=str(item.get("id") or item.get("threadId") or ""),
                    cwd=str(item.get("cwd") or item.get("path") or ""),
                    preview=str(item.get("preview") or ""),
                    status=str(item.get("status") or "idle"),
                    name=item.get("name"),
                    path=item.get("path"),
                )
            )
        return snapshots

    def get(self, thread_id: str) -> NativeThreadSnapshot | None:
        try:
            thread = self.store.get_thread(thread_id)
        except KeyError:
            return None
        return self._to_snapshot(thread)

    def list_threads(self, *, cwd: str | None = None) -> list[NativeThreadSnapshot]:
        snapshots = [self._to_snapshot(thread) for thread in self.store.list_threads()]
        if cwd is None:
            return snapshots
        normalized = self._normalize_cwd(cwd)
        return [
            snapshot
            for snapshot in snapshots
            if self._normalize_cwd(snapshot.cwd) == normalized
        ]

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        if ThreadDirectory._looks_like_windows_path(cwd):
            return ntpath.normcase(ntpath.normpath(cwd))
        return posixpath.normpath(cwd)

    @staticmethod
    def _looks_like_windows_path(cwd: str) -> bool:
        return (
            len(cwd) >= 2
            and cwd[1] == ":"
            or "\\" in cwd
            or cwd.startswith("\\\\")
        )

    def _to_snapshot(self, thread) -> NativeThreadSnapshot:
        return NativeThreadSnapshot(
            thread_id=thread.thread_id,
            cwd=thread.cwd,
            preview=thread.preview,
            status=thread.status,
            name=thread.name,
            path=thread.path,
        )

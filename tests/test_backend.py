from __future__ import annotations

import pytest

from imcodex.appserver import CodexBackend
from imcodex.store import ConversationStore


class FakeClient:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []

    async def list_threads(self, **params):
        self.list_calls.append(params)
        return {
            "data": [
                {
                    "id": "thr_same_cwd",
                    "cwd": r"D:\desktop\imcodex",
                    "preview": "same cwd",
                    "status": {"type": "notLoaded"},
                    "source": "vscode",
                },
                {
                    "id": "thr_other",
                    "cwd": r"D:\elsewhere",
                    "preview": "other cwd",
                    "status": {"type": "notLoaded"},
                    "source": "cli",
                },
            ],
            "nextCursor": None,
        }

    async def read_thread(self, thread_id: str):
        raise AssertionError(f"read_thread should not be called for listed thread {thread_id}")


@pytest.mark.asyncio
async def test_list_threads_accepts_data_key_and_prioritizes_preferred_cwd() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\desktop\imcodex")
    client = FakeClient()
    backend = CodexBackend(client=client, store=store, service_name="imcodex-test")

    threads = await backend.list_threads("qq", "conv-1")

    assert [thread.thread_id for thread in threads] == ["thr_same_cwd", "thr_other"]
    assert client.list_calls == [{"sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "appServer"]}]

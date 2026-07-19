from __future__ import annotations

import json

import pytest

from imcodex.appserver.client import AppServerError
from imcodex.appserver.thread_dynamic_tools import (
    NATIVE_THREAD_DYNAMIC_TOOL_NAMES,
    native_thread_dynamic_tool_specs,
)
from imcodex.bridge.native_thread_tools import NativeThreadToolAdapter
from imcodex.store import ConversationStore


class RecordingBackend:
    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    async def call_native(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, dict(params or {})))
        result = self.responses[method].pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def content(response: dict) -> dict:
    return json.loads(response["contentItems"][0]["text"])


def test_native_thread_dynamic_tool_specs_cover_every_native_adapter() -> None:
    specs = native_thread_dynamic_tool_specs()

    assert {spec["name"] for spec in specs} == NATIVE_THREAD_DYNAMIC_TOOL_NAMES
    assert all(spec["type"] == "function" for spec in specs)
    assert all(spec["inputSchema"]["type"] == "object" for spec in specs)
    specs[0]["name"] = "mutated"
    assert {spec["name"] for spec in native_thread_dynamic_tool_specs()} == (
        NATIVE_THREAD_DYNAMIC_TOOL_NAMES
    )


@pytest.mark.asyncio
async def test_list_threads_matches_desktop_model_facing_shape() -> None:
    backend = RecordingBackend(
        {
            "thread/list": [
                {
                    "data": [
                        {
                            "id": "thr_api",
                            "name": "API extraction",
                            "preview": "Extract the API",
                            "cwd": "/work/imcodex",
                            "status": {"type": "active", "activeFlags": []},
                            "createdAt": 10,
                            "updatedAt": 20,
                        }
                    ]
                }
            ]
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "list_threads",
        {"query": "API", "limit": 4},
        source_thread_id="thr_source",
    )

    assert response["success"] is True
    assert content(response) == {
        "schemaVersion": 2,
        "query": "API",
        "threads": [
            {
                "id": "thr_api",
                "hostId": "local",
                "title": "API extraction",
                "description": "imcodex",
                "preview": "Extract the API",
                "status": "active",
                "hasUnreadTurn": False,
                "cwd": "/work/imcodex",
                "createdAt": 10,
                "updatedAt": 20,
            }
        ],
        "unavailableHosts": [],
    }
    assert backend.calls == [
        (
            "thread/list",
            {
                "limit": 4,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "sourceKinds": ["cli", "vscode", "appServer"],
                "searchTerm": "API",
            },
        )
    ]


@pytest.mark.asyncio
async def test_read_thread_pages_turns_and_hides_outputs_by_default() -> None:
    backend = RecordingBackend(
        {
            "thread/read": [
                {
                    "thread": {
                        "id": "thr_api",
                        "preview": "Extract the API",
                        "cwd": "/work/imcodex",
                        "status": {"type": "idle"},
                    }
                }
            ],
            "thread/turns/list": [
                {
                    "data": [
                        {
                            "id": "turn_2",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "commandExecution",
                                    "command": "pytest",
                                    "aggregatedOutput": "secret output",
                                },
                                {
                                    "type": "mcpToolCall",
                                    "tool": "lookup",
                                    "result": {"secret": "value"},
                                },
                            ],
                        },
                    ],
                    "nextCursor": "older-turns",
                }
            ],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "read_thread",
        {"threadId": "thr_api", "turnLimit": 1},
        source_thread_id="thr_source",
    )

    result = content(response)
    assert result["page"] == {
        "order": "newest_first",
        "limit": 1,
        "nextCursor": "older-turns",
        "hasMore": True,
    }
    assert result["turns"][0]["id"] == "turn_2"
    assert "aggregatedOutput" not in result["turns"][0]["items"][0]
    assert "result" not in result["turns"][0]["items"][1]
    assert backend.calls == [
        ("thread/read", {"threadId": "thr_api"}),
        (
            "thread/turns/list",
            {
                "threadId": "thr_api",
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_read_thread_bounds_large_model_text() -> None:
    backend = RecordingBackend(
        {
            "thread/read": [{"thread": {"id": "thr_api", "status": {"type": "idle"}}}],
            "thread/turns/list": [
                {
                    "data": [
                        {
                            "id": "turn_large",
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": "x" * 20_000}],
                        }
                    ],
                    "nextCursor": None,
                }
            ],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "read_thread",
        {"threadId": "thr_api"},
        source_thread_id="thr_source",
    )

    assert response["success"] is True
    text = content(response)["turns"][0]["items"][0]["text"]
    assert len(text) < 8_100
    assert text.endswith("[truncated]")


@pytest.mark.asyncio
async def test_dynamic_tool_response_has_an_overall_budget() -> None:
    backend = RecordingBackend(
        {
            "thread/list": [
                {
                    "data": [
                        {"id": f"thr_{index}", "preview": "x" * 20_000, "status": {"type": "idle"}}
                        for index in range(10)
                    ]
                }
            ]
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call("list_threads", {}, source_thread_id="thr_source")

    assert response["success"] is False
    assert "safe response budget" in response["contentItems"][0]["text"]


@pytest.mark.asyncio
async def test_dynamic_tool_failure_message_is_bounded() -> None:
    backend = RecordingBackend({})
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "read_thread",
        {"threadId": "thr_api", "hostId": "x" * 20_000},
        source_thread_id="thr_source",
    )

    assert response["success"] is False
    assert len(response["contentItems"][0]["text"]) < 2_100
    assert response["contentItems"][0]["text"].endswith("[truncated]")


@pytest.mark.asyncio
async def test_send_message_resumes_target_and_starts_native_turn() -> None:
    backend = RecordingBackend(
        {
            "thread/resume": [{"thread": {"id": "thr_target", "turns": []}}],
            "turn/start": [{"turn": {"id": "turn_new", "status": "inProgress"}}],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "send_message_to_thread",
        {
            "threadId": "thr_target",
            "prompt": "拆解这个接口",
            "model": "gpt-5.4",
            "thinking": "high",
        },
        source_thread_id="thr_source",
    )

    assert content(response) == {"threadId": "thr_target"}
    assert backend.calls == [
        ("thread/resume", {"threadId": "thr_target"}),
        (
            "turn/start",
            {
                "threadId": "thr_target",
                "input": [{"type": "text", "text": "拆解这个接口"}],
                "summary": "concise",
                "model": "gpt-5.4",
                "effort": "high",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_send_message_steers_an_active_target_turn() -> None:
    backend = RecordingBackend(
        {
            "thread/resume": [
                {
                    "thread": {
                        "id": "thr_target",
                        "turns": [{"id": "turn_active", "status": "inProgress"}],
                    }
                }
            ],
            "turn/steer": [{"turnId": "turn_active"}],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "send_message_to_thread",
        {"threadId": "thr_target", "prompt": "再加一项"},
        source_thread_id="thr_source",
    )

    assert response["success"] is True
    assert backend.calls[-1] == (
        "turn/steer",
        {
            "threadId": "thr_target",
            "expectedTurnId": "turn_active",
            "input": [{"type": "text", "text": "再加一项"}],
        },
    )


@pytest.mark.asyncio
async def test_send_message_rejects_overrides_for_an_active_target_turn() -> None:
    backend = RecordingBackend(
        {
            "thread/resume": [
                {
                    "thread": {
                        "id": "thr_target",
                        "turns": [{"id": "turn_active", "status": "inProgress"}],
                    }
                }
            ],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "send_message_to_thread",
        {"threadId": "thr_target", "prompt": "再加一项", "thinking": "high"},
        source_thread_id="thr_source",
    )

    assert response["success"] is False
    assert "cannot apply model or thinking overrides" in response["contentItems"][0]["text"]
    assert backend.calls == [("thread/resume", {"threadId": "thr_target"})]


@pytest.mark.asyncio
async def test_create_thread_uses_source_cwd_and_recursively_registers_tools() -> None:
    backend = RecordingBackend(
        {
            "thread/read": [
                {"thread": {"id": "thr_source", "cwd": r"D:\work\imcodex"}}
            ],
            "thread/start": [{"thread": {"id": "thr_child", "cwd": r"D:\work\imcodex"}}],
            "turn/start": [{"turn": {"id": "turn_child", "status": "inProgress"}}],
        }
    )
    backend.service_name = "imcodex-test"
    backend.thread_dynamic_tools = [
        spec
        for spec in native_thread_dynamic_tool_specs()
        if spec["name"] in {"create_thread", "list_threads"}
    ]
    backend.store = ConversationStore(clock=lambda: 1.0)
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "create_thread",
        {"prompt": "检查 Windows 回归", "model": "gpt-5.4", "thinking": "high"},
        source_thread_id="thr_source",
    )

    assert response["success"] is True
    assert content(response) == {"threadId": "thr_child"}
    assert backend.calls[0] == ("thread/read", {"threadId": "thr_source"})
    start_method, start_params = backend.calls[1]
    assert start_method == "thread/start"
    assert start_params["cwd"] == r"D:\work\imcodex"
    assert start_params["serviceName"] == "imcodex-test"
    assert {spec["name"] for spec in start_params["dynamicTools"]} == {
        "create_thread",
        "list_threads",
    }
    assert backend.store.is_native_thread_tool_thread("thr_child") is True
    assert backend.calls[2] == (
        "turn/start",
        {
            "threadId": "thr_child",
            "input": [{"type": "text", "text": "检查 Windows 回归"}],
            "summary": "concise",
            "model": "gpt-5.4",
            "effort": "high",
        },
    )


@pytest.mark.asyncio
async def test_create_thread_reports_created_id_when_initial_turn_is_ambiguous() -> None:
    backend = RecordingBackend(
        {
            "thread/read": [{"thread": {"id": "thr_source", "cwd": "/work/imcodex"}}],
            "thread/start": [{"thread": {"id": "thr_child", "cwd": "/work/imcodex"}}],
            "turn/start": [AppServerError("connection closed")],
        }
    )
    backend.service_name = "imcodex-test"
    backend.thread_dynamic_tools = native_thread_dynamic_tool_specs()
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "create_thread",
        {"prompt": "检查回归"},
        source_thread_id="thr_source",
    )

    assert response["success"] is False
    failure = content(response)
    assert failure["threadId"] == "thr_child"
    assert failure["initialTurnStatus"] == "unknown"
    assert "instead of retrying" in failure["recovery"]


@pytest.mark.asyncio
async def test_create_thread_reports_created_id_when_provenance_commit_fails() -> None:
    class FailingStore:
        async def claim_native_thread_tool_thread(self, _thread_id: str) -> None:
            raise OSError("disk full")

    backend = RecordingBackend(
        {
            "thread/read": [{"thread": {"id": "thr_source", "cwd": "/work/imcodex"}}],
            "thread/start": [{"thread": {"id": "thr_child", "cwd": "/work/imcodex"}}],
        }
    )
    backend.service_name = "imcodex-test"
    backend.thread_dynamic_tools = native_thread_dynamic_tool_specs()
    backend.store = FailingStore()
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call(
        "create_thread",
        {"prompt": "检查回归"},
        source_thread_id="thr_source",
    )

    assert response["success"] is False
    failure = content(response)
    assert failure["threadId"] == "thr_child"
    assert failure["phase"] == "toolHostRegistration"
    assert failure["initialTurnStatus"] == "notStarted"
    assert not any(method == "turn/start" for method, _params in backend.calls)


@pytest.mark.asyncio
async def test_native_mutations_map_without_bridge_owned_thread_state() -> None:
    backend = RecordingBackend(
        {
            "thread/fork": [{"thread": {"id": "thr_fork"}}],
            "thread/name/set": [{}],
            "thread/archive": [{}],
            "thread/unarchive": [{"thread": {"id": "thr_fork"}}],
        }
    )
    adapter = NativeThreadToolAdapter(backend=backend)

    forked = await adapter.call("fork_thread", {}, source_thread_id="thr_source")
    renamed = await adapter.call(
        "set_thread_title",
        {"threadId": "thr_fork", "title": "接口拆解"},
        source_thread_id="thr_source",
    )
    archived = await adapter.call(
        "set_thread_archived",
        {"threadId": "thr_fork", "archived": True},
        source_thread_id="thr_source",
    )
    unarchived = await adapter.call(
        "set_thread_archived",
        {"threadId": "thr_fork", "archived": False},
        source_thread_id="thr_source",
    )

    assert content(forked)["threadId"] == "thr_fork"
    assert content(renamed) == {"threadId": "thr_fork", "title": "接口拆解"}
    assert content(archived) == {"threadId": "thr_fork", "archived": True}
    assert content(unarchived) == {"threadId": "thr_fork", "archived": False}
    assert backend.calls == [
        ("thread/fork", {"threadId": "thr_source"}),
        ("thread/name/set", {"threadId": "thr_fork", "name": "接口拆解"}),
        ("thread/archive", {"threadId": "thr_fork"}),
        ("thread/unarchive", {"threadId": "thr_fork"}),
    ]


@pytest.mark.asyncio
async def test_desktop_only_tool_fails_without_inventing_local_state() -> None:
    backend = RecordingBackend({})
    adapter = NativeThreadToolAdapter(backend=backend)

    response = await adapter.call("set_thread_pinned", {"pinned": True}, source_thread_id="thr_source")

    assert response["success"] is False
    assert "Codex Desktop-owned state" in response["contentItems"][0]["text"]
    assert backend.calls == []

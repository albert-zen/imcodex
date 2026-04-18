from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from imcodex.application import create_application
from imcodex.models import NativeThreadSnapshot
from imcodex.store import ConversationStore


class _StubClient:
    def __init__(self) -> None:
        self.connection_mode = "shared-ws"
        self.initialized = True
        self._pending_server_requests = {"native-request-abcdef": {"id": 99}}
        self.reset_calls = 0

    def add_notification_handler(self, _handler) -> None:
        return None

    def add_server_request_handler(self, _handler) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _reset_connection(self) -> None:
        self.reset_calls += 1
        self._pending_server_requests.clear()


async def _noop_async() -> None:
    return None


def test_debug_api_exposes_runtime_and_conversation_state(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    health_path.write_text(
        json.dumps({"instance_id": "inst-1", "status": "healthy", "http": {"listening": True}}),
        encoding="utf-8",
    )
    store = ConversationStore(clock=lambda: 1.0, state_path=tmp_path / "state.json")
    store.set_bootstrap_cwd("debug", "conv-1", r"D:\desktop\imcodex-debug-lab\cwd\debug-1")
    store.bind_thread_with_cwd("debug", "conv-1", "thr-1", r"D:\desktop\imcodex-debug-lab\cwd\debug-1")
    store.note_thread_snapshot(
        NativeThreadSnapshot(
            thread_id="thr-1",
            cwd=r"D:\desktop\imcodex-debug-lab\cwd\debug-1",
            preview="debug thread",
            status="idle",
        )
    )
    store.note_active_turn("thr-1", "turn-1", "inProgress")
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="debug",
        conversation_id="conv-1",
        thread_id="thr-1",
        turn_id="turn-1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        payload={"reason": "Need approval"},
    )

    client = _StubClient()
    runtime = SimpleNamespace(
        client=client,
        service=SimpleNamespace(store=store, backend=SimpleNamespace(client=client)),
        observability=SimpleNamespace(
            context=SimpleNamespace(instance_id="inst-1"),
            paths=SimpleNamespace(current_health_path=health_path),
        ),
        managed_channels=[],
        start=_noop_async,
        stop=_noop_async,
    )
    settings = SimpleNamespace(debug_api_enabled=True)

    app = create_application(settings=settings, runtime=runtime)
    test_client = TestClient(app)

    runtime_response = test_client.get("/api/debug/runtime")
    conversation_response = test_client.get("/api/debug/conversation/debug/conv-1")

    assert runtime_response.status_code == 200
    assert runtime_response.json()["appserver"]["pending_server_request_ids"] == ["native-request-abcdef"]
    assert conversation_response.status_code == 200
    body = conversation_response.json()
    assert body["binding"]["thread_id"] == "thr-1"
    assert body["active_turn"]["turn_id"] == "turn-1"
    assert body["pending_requests"][0]["request_id"] == "native-request-abcdef"


def test_debug_api_can_inject_pending_request_and_active_turn(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps({"instance_id": "inst-1", "status": "healthy"}), encoding="utf-8")
    store = ConversationStore(clock=lambda: 1.0, state_path=tmp_path / "state.json")
    client = _StubClient()
    runtime = SimpleNamespace(
        client=client,
        service=SimpleNamespace(store=store, backend=SimpleNamespace(client=client)),
        observability=SimpleNamespace(
            context=SimpleNamespace(instance_id="inst-1"),
            paths=SimpleNamespace(current_health_path=health_path),
        ),
        managed_channels=[],
        start=_noop_async,
        stop=_noop_async,
    )
    settings = SimpleNamespace(debug_api_enabled=True)

    app = create_application(settings=settings, runtime=runtime)
    test_client = TestClient(app)

    bind_response = test_client.post(
        "/api/debug/inject/binding",
        json={
            "channel_id": "debug",
            "conversation_id": "conv-1",
            "thread_id": "thr-1",
            "cwd": r"D:\desktop\imcodex-debug-lab\cwd\debug-1",
            "preview": "debug thread",
            "status": "idle",
        },
    )
    turn_response = test_client.post(
        "/api/debug/inject/active-turn",
        json={"thread_id": "thr-1", "turn_id": "turn-1", "status": "inProgress"},
    )
    request_response = test_client.post(
        "/api/debug/inject/pending-request",
        json={
            "request_id": "native-request-abcdef",
            "channel_id": "debug",
            "conversation_id": "conv-1",
            "thread_id": "thr-1",
            "turn_id": "turn-1",
            "kind": "approval",
            "request_method": "item/commandExecution/requestApproval",
            "payload": {"reason": "Need approval"},
        },
    )
    inspect_response = test_client.get("/api/debug/conversation/debug/conv-1")

    assert bind_response.status_code == 200
    assert turn_response.status_code == 200
    assert request_response.status_code == 200
    assert inspect_response.json()["active_turn"]["turn_id"] == "turn-1"
    assert inspect_response.json()["pending_requests"][0]["request_id"] == "native-request-abcdef"


def test_debug_api_can_inject_client_pending_request_and_force_reset(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps({"instance_id": "inst-1", "status": "healthy"}), encoding="utf-8")
    store = ConversationStore(clock=lambda: 1.0, state_path=tmp_path / "state.json")
    client = _StubClient()
    runtime = SimpleNamespace(
        client=client,
        service=SimpleNamespace(store=store, backend=SimpleNamespace(client=client)),
        observability=SimpleNamespace(
            context=SimpleNamespace(instance_id="inst-1"),
            paths=SimpleNamespace(current_health_path=health_path),
        ),
        managed_channels=[],
        start=_noop_async,
        stop=_noop_async,
    )
    settings = SimpleNamespace(debug_api_enabled=True)

    app = create_application(settings=settings, runtime=runtime)
    test_client = TestClient(app)

    inject_response = test_client.post(
        "/api/debug/inject/client-pending-request",
        json={"request_id": "native-request-extra", "jsonrpc_id": 123},
    )
    runtime_before = test_client.get("/api/debug/runtime")
    reset_response = test_client.post("/api/debug/force/client-reset")
    runtime_after = test_client.get("/api/debug/runtime")

    assert inject_response.status_code == 200
    assert runtime_before.json()["appserver"]["pending_server_request_ids"] == [
        "native-request-abcdef",
        "native-request-extra",
    ]
    assert reset_response.status_code == 200
    assert reset_response.json()["ok"] is True
    assert client.reset_calls == 1
    assert runtime_after.json()["appserver"]["pending_server_request_ids"] == []

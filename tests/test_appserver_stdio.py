from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import websockets

from imcodex.appserver import (
    AppServerClient,
    AppServerError,
    AppServerSupervisor,
    summarize_text,
    summarize_transport_message,
)
from imcodex.appserver.client import DEFAULT_OPT_OUT_NOTIFICATION_METHODS
from imcodex.appserver.retry import RetryBackoff
from imcodex.appserver.supervisor import (
    DEFAULT_UNIX_WEBSOCKET_URI,
    WS_MAX_SIZE,
    HealthProbeResult,
    UnsupportedUnixSocketError,
    default_app_server_control_socket,
    derive_health_probe_urls,
    resolve_unix_socket_path,
)


class FakeStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = bytearray()
        self._eof = False

    async def _fill_buffer(self) -> bool:
        if self._eof:
            return False
        chunk = await self.lines.get()
        if not chunk:
            self._eof = True
            return False
        self._buffer.extend(chunk)
        return True

    async def readline(self) -> bytes:
        while b"\n" not in self._buffer:
            if not await self._fill_buffer():
                if not self._buffer:
                    return b""
                data = bytes(self._buffer)
                self._buffer.clear()
                return data
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        return bytes(line) + b"\n"

    async def read(self, n: int = -1) -> bytes:
        while not self._buffer:
            if not await self._fill_buffer():
                return b""
        if n < 0 or n >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data


class LimitFailingStdout(FakeStdout):
    def __init__(self, *, limit: int) -> None:
        super().__init__()
        self.limit = limit

    async def readline(self) -> bytes:
        while b"\n" not in self._buffer:
            if len(self._buffer) > self.limit:
                raise ValueError("Separator is not found, and chunk exceed the limit")
            if not await self._fill_buffer():
                if not self._buffer:
                    return b""
                data = bytes(self._buffer)
                self._buffer.clear()
                return data
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        if len(line) > self.limit:
            raise ValueError("Separator is not found, and chunk exceed the limit")
        return bytes(line) + b"\n"


class FakeStdin:
    def __init__(self, process: "ScriptedProcess") -> None:
        self.process = process
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            self.process.on_input(line.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.closed = True


class ScriptedProcess:
    def __init__(self, scripts: dict[str, list[dict]]) -> None:
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.stderr = FakeStdout()
        self.scripts = scripts
        self.closed = False
        self.returncode: int | None = None
        self.sent: list[dict] = []

    def on_input(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        method = payload.get("method")
        if method == "initialized":
            return
        if method is None:
            return
        for message in self.scripts.get(method, []):
            self.stdout.lines.put_nowait((json.dumps(self._prepare_scripted_message(payload, message)) + "\n").encode("utf-8"))

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.lines.put_nowait(b"")

    async def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def _prepare_scripted_message(self, request: dict, scripted: dict) -> dict:
        if "method" in scripted:
            return dict(scripted)
        response = dict(scripted)
        if "id" in request:
            response["id"] = request["id"]
        return response


class LimitFailingProcess(ScriptedProcess):
    def __init__(self, scripts: dict[str, list[dict]], *, line_limit: int) -> None:
        super().__init__(scripts)
        self.stdout = LimitFailingStdout(limit=line_limit)
        self.stdin = FakeStdin(self)


class ScriptedWebSocket:
    def __init__(self, scripts: dict[str, list[dict]]) -> None:
        self.scripts = scripts
        self.sent: list[dict] = []
        self.messages: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        payload = json.loads(data)
        self.sent.append(payload)
        method = payload.get("method")
        if method == "initialized":
            return
        if method is None:
            return
        for message in self.scripts.get(method, []):
            await self.messages.put(json.dumps(self._prepare_scripted_message(payload, message)))

    async def recv(self) -> str:
        message = await self.messages.get()
        if isinstance(message, Exception):
            self.closed = True
            raise message
        return message

    async def close(self) -> None:
        self.closed = True

    def _prepare_scripted_message(self, request: dict, scripted: dict) -> dict:
        if "method" in scripted:
            return dict(scripted)
        response = dict(scripted)
        if "id" in request:
            response["id"] = request["id"]
        return response


class OverloadThenOkProcess(ScriptedProcess):
    def __init__(self) -> None:
        super().__init__({})
        self.thread_list_attempts = 0

    def on_input(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        method = payload.get("method")
        if method == "initialized" or method is None:
            return
        if method == "initialize":
            self.stdout.lines.put_nowait((json.dumps({"id": payload["id"], "result": {"ok": True}}) + "\n").encode("utf-8"))
            return
        if method == "thread/list":
            self.thread_list_attempts += 1
            if self.thread_list_attempts == 1:
                response = {
                    "id": payload["id"],
                    "error": {"code": -32001, "message": "Server overloaded; retry later"},
                }
            else:
                response = {"id": payload["id"], "result": {"threads": []}}
            self.stdout.lines.put_nowait((json.dumps(response) + "\n").encode("utf-8"))


@pytest.mark.asyncio
async def test_scripted_helpers_mirror_request_ids_for_responses() -> None:
    process = ScriptedProcess({"thread/list": [{"id": 999, "result": {"threads": []}}]})
    process.on_input(json.dumps({"id": 7, "method": "thread/list", "params": {}}))
    echoed = json.loads((await process.stdout.readline()).decode("utf-8"))
    assert echoed["id"] == 7

    websocket = ScriptedWebSocket({"thread/list": [{"id": 999, "result": {"threads": []}}]})
    await websocket.send(json.dumps({"id": 8, "method": "thread/list", "params": {}}))
    echoed_ws = json.loads(await websocket.recv())
    assert echoed_ws["id"] == 8


@pytest.mark.asyncio
async def test_stdio_client_initializes_dispatches_notifications_and_replies_to_server_request() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/start": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": "D:/repo/app",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                },
                {
                    "id": 99,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "questions": [{"id": "color", "question": "Favorite color?"}],
                    },
                },
            ],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    captured_requests: list[dict] = []
    client.add_server_request_handler(captured_requests.append)

    result = await client.start_thread(cwd="D:/repo/app")
    await asyncio.sleep(0)

    assert result["thread"]["id"] == "thr_1"
    assert captured_requests[0]["method"] == "item/tool/requestUserInput"
    assert captured_requests[0]["params"]["_request_id"] == "99"
    assert captured_requests[0]["params"]["_transport_request_id"] == 99
    assert captured_requests[0]["params"]["_connection_epoch"] == 1

    await client.reply_to_transport_request(99, {"answers": {"color": {"answers": ["blue"]}}})

    assert process.sent[-1] == {
        "id": 99,
        "result": {"answers": {"color": {"answers": ["blue"]}}},
    }


@pytest.mark.asyncio
async def test_stdio_client_dispatches_server_request_with_non_object_params() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/start": [
                {"id": 2, "result": {"thread": {"id": "thr_1", "cwd": "D:/repo/app", "status": "idle"}}},
                {"id": 98, "method": "future/native/requestThing", "params": ["unexpected", "shape"]},
            ],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    captured_requests: list[dict] = []
    client.add_server_request_handler(captured_requests.append)

    await client.start_thread(cwd="D:/repo/app")
    await asyncio.sleep(0)

    assert captured_requests[0]["method"] == "future/native/requestThing"
    assert captured_requests[0]["params"]["_raw_params"] == ["unexpected", "shape"]
    assert captured_requests[0]["params"]["_request_id"] == "98"
    assert captured_requests[0]["params"]["_transport_request_id"] == 98
    await client.close()


@pytest.mark.asyncio
async def test_initialize_does_not_enable_experimental_api_by_default() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.initialize()

    capabilities = process.sent[0]["params"]["capabilities"]
    assert capabilities["optOutNotificationMethods"] == list(DEFAULT_OPT_OUT_NOTIFICATION_METHODS)
    assert "experimentalApi" not in capabilities
    await client.close()


@pytest.mark.asyncio
async def test_initialize_enables_experimental_api_only_when_configured() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        experimental_api_enabled=True,
    )

    await client.initialize()

    capabilities = process.sent[0]["params"]["capabilities"]
    assert capabilities["optOutNotificationMethods"] == list(DEFAULT_OPT_OUT_NOTIFICATION_METHODS)
    assert capabilities["experimentalApi"] is True
    await client.close()


@pytest.mark.asyncio
async def test_lightweight_recovery_resume_falls_back_for_older_app_servers(monkeypatch) -> None:
    supervisor = AppServerSupervisor(app_server_url="stdio://")
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        experimental_api_enabled=True,
    )
    calls: list[tuple[str, dict]] = []

    async def request(method: str, params: dict) -> dict:
        calls.append((method, params))
        if params.get("excludeTurns"):
            raise AppServerError("invalid params: unknown field excludeTurns", code=-32602)
        return {"thread": {"id": "thr_1", "status": "idle", "turns": []}}

    monkeypatch.setattr(client, "_request", request)

    result = await client.resume_thread_for_recovery(thread_id="thr_1")

    assert result["thread"]["id"] == "thr_1"
    assert calls == [
        (
            "thread/resume",
            {
                "threadId": "thr_1",
                "excludeTurns": True,
                "initialTurnsPage": {"limit": 4},
            },
        ),
        ("thread/resume", {"threadId": "thr_1"}),
    ]


def test_resume_initial_turns_page_is_merged_in_chronological_order() -> None:
    client = AppServerClient(
        supervisor=AppServerSupervisor(app_server_url="stdio://"),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    result = {
        "thread": {"id": "thr_1", "status": "inProgress", "turns": []},
        "initialTurnsPage": {
            "data": [
                {"id": "turn_new", "status": "inProgress"},
                {"id": "turn_old", "status": "completed"},
            ]
        },
    }

    normalized = client._normalize_result("thread/resume", result)

    assert [turn["id"] for turn in normalized["thread"]["turns"]] == [
        "turn_old",
        "turn_new",
    ]


@pytest.mark.asyncio
async def test_ready_handler_can_report_degraded_native_rehydration(monkeypatch) -> None:
    health_updates: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.mark_appserver_health",
        lambda **payload: health_updates.append(payload),
    )
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            app_server_url="stdio://",
            spawn_process=lambda *_args: process,
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(
        lambda _epoch: {
            "status": "degraded",
            "rehydration": {"total": 1, "succeeded": 0, "failed": 1, "unverified": 0},
        }
    )

    await client.initialize()

    assert health_updates[-1]["status"] == "degraded"
    assert health_updates[-1]["rehydration"]["failed"] == 1
    assert client.connection_facts()["status"] == "degraded"
    assert client.connection_facts()["rehydration"]["failed"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_background_reconnect_preserves_degraded_rehydration_health(monkeypatch) -> None:
    observed_health: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.mark_appserver_health",
        lambda **payload: observed_health.append(payload),
    )
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    sockets = iter([first, second])
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            app_server_url="ws://127.0.0.1:9001",
            websocket_factory=lambda _url: next(sockets),
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    def ready_health(epoch: int) -> dict | None:
        if epoch != 2:
            return None
        return {
            "status": "degraded",
            "rehydration": {"total": 1, "succeeded": 0, "failed": 1, "unverified": 0},
        }

    client.add_connection_ready_handler(ready_health)
    await client.initialize()
    listener_task = client._listener_task
    assert listener_task is not None
    first.messages.put_nowait(ConnectionError("socket closed"))

    await asyncio.wait_for(listener_task, timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)

    assert client.connection_facts()["status"] == "degraded"
    assert observed_health[-1]["status"] == "degraded"
    assert observed_health[-1]["rehydration"]["failed"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_client_reads_response_while_notification_handler_is_still_running() -> None:
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    process = ScriptedProcess(
        {
            "initialize": [
                {
                    "method": "thread/status/changed",
                    "params": {"threadId": "thr_1", "status": "idle"},
                },
                {"id": 1, "result": {"ok": True}},
            ],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        request_timeout_s=0.2,
    )

    async def slow_handler(_notification: dict) -> None:
        handler_started.set()
        await release_handler.wait()

    client.add_notification_handler(slow_handler)

    result = await client.list_threads()

    assert result == {"threads": []}
    assert handler_started.is_set()
    release_handler.set()
    await client.close()


@pytest.mark.asyncio
async def test_server_request_dispatch_is_not_blocked_by_slow_notifications() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    notification_started = asyncio.Event()
    release_notification = asyncio.Event()
    request_received = asyncio.Event()
    captured_requests: list[dict] = []
    supervisor = AppServerSupervisor(
        app_server_url="stdio://",
        spawn_process=lambda *_args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    async def slow_notification(_notification: dict) -> None:
        notification_started.set()
        await release_notification.wait()

    def capture_request(request: dict) -> None:
        captured_requests.append(request)
        request_received.set()

    client.add_notification_handler(slow_notification)
    client.add_server_request_handler(capture_request)
    await client.initialize()
    process.stdout.lines.put_nowait(
        b'{"method":"thread/status/changed","params":{"threadId":"thr_1"}}\n'
    )
    await asyncio.wait_for(notification_started.wait(), timeout=1)
    process.stdout.lines.put_nowait(
        b'{"id":91,"method":"item/commandExecution/requestApproval","params":{"threadId":"thr_1"}}\n'
    )

    await asyncio.wait_for(request_received.wait(), timeout=1)

    assert captured_requests[0]["id"] == 91
    release_notification.set()
    await client.close()


@pytest.mark.asyncio
async def test_server_request_resolution_preserves_wire_order_with_its_request() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    resolution_seen = asyncio.Event()
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            app_server_url="stdio://",
            spawn_process=lambda *_args: process,
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    async def slow_request(_request: dict) -> None:
        request_started.set()
        await release_request.wait()

    def capture_notification(notification: dict) -> None:
        if notification.get("method") == "serverRequest/resolved":
            resolution_seen.set()

    client.add_server_request_handler(slow_request)
    client.add_notification_handler(capture_notification)
    await client.initialize()
    process.stdout.lines.put_nowait(
        b'{"id":91,"method":"item/commandExecution/requestApproval","params":{}}\n'
    )
    await asyncio.wait_for(request_started.wait(), timeout=1)
    process.stdout.lines.put_nowait(
        b'{"method":"serverRequest/resolved","params":{"requestId":"91"}}\n'
    )

    await asyncio.sleep(0)
    assert resolution_seen.is_set() is False
    release_request.set()
    await asyncio.wait_for(resolution_seen.wait(), timeout=1)
    await client.close()


@pytest.mark.asyncio
async def test_notification_queue_overflow_resets_instead_of_growing_unbounded(monkeypatch) -> None:
    observed_events: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.emit_event",
        lambda **payload: observed_events.append(payload),
    )
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    notification_started = asyncio.Event()
    supervisor = AppServerSupervisor(
        app_server_url="stdio://",
        spawn_process=lambda *_args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        notification_queue_size=1,
    )

    async def blocked_notification(_notification: dict) -> None:
        notification_started.set()
        await asyncio.Event().wait()

    client.add_notification_handler(blocked_notification)
    await client.initialize()
    process.stdout.lines.put_nowait(b'{"method":"event/one","params":{}}\n')
    await asyncio.wait_for(notification_started.wait(), timeout=1)
    listener_task = client._listener_task
    assert listener_task is not None
    process.stdout.lines.put_nowait(b'{"method":"event/two","params":{}}\n')
    process.stdout.lines.put_nowait(b'{"method":"event/three","params":{}}\n')

    await asyncio.wait_for(listener_task, timeout=1)

    assert client.connection_mode == "disconnected"
    assert client._dispatch_queue is None
    assert any(event.get("event") == "appserver.dispatch.overflow" for event in observed_events)
    await client.close()


@pytest.mark.asyncio
async def test_stdio_client_replies_using_transport_request_id() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/start": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": "D:/repo/app",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                },
                {
                    "id": 99,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "requestId": "native-request-abcdef",
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "questions": [{"id": "color", "question": "Favorite color?"}],
                    },
                },
            ],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.start_thread(cwd="D:/repo/app")
    await asyncio.sleep(0)
    await client.reply_to_transport_request(99, {"answers": {"color": {"answers": ["blue"]}}})

    assert process.sent[-1] == {
        "id": 99,
        "result": {"answers": {"color": {"answers": ["blue"]}}},
    }


@pytest.mark.asyncio
async def test_stdio_client_respawns_after_process_eof() -> None:
    first = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    second = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    processes = iter([first, second])
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: next(processes),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.connect()
    first.stdout.lines.put_nowait(b"")
    await asyncio.sleep(0)
    result = await client.list_threads()

    assert result == {"threads": []}
    assert second.sent[0]["method"] == "initialize"


@pytest.mark.asyncio
async def test_stdio_client_increments_connection_epoch_after_respawn() -> None:
    first = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    second = ScriptedProcess(
        {
            "initialize": [{"id": 3, "result": {"ok": True}}],
            "thread/list": [{"id": 4, "result": {"threads": []}}],
        }
    )
    processes = iter([first, second])
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: next(processes),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.connect()
    assert client.connection_epoch == 1
    first.stdout.lines.put_nowait(b"")
    await asyncio.sleep(0)
    await client.list_threads()

    assert client.connection_epoch == 2


@pytest.mark.asyncio
async def test_stdio_client_fails_inflight_request_immediately_when_pipe_closes() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.initialize()
    pending = asyncio.create_task(client.list_threads())
    await asyncio.sleep(0)
    process.stdout.lines.put_nowait(b"")

    with pytest.raises(AppServerError, match="connection closed"):
        await pending


@pytest.mark.asyncio
async def test_stdio_client_handles_oversized_jsonl_messages_without_readline_limits() -> None:
    huge_preview = "x" * (2 * 1024 * 1024)
    process = LimitFailingProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_big",
                            "cwd": "D:/repo/app",
                            "preview": huge_preview,
                            "status": "idle",
                        }
                    },
                }
            ],
        },
        line_limit=1024,
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.resume_thread(thread_id="thr_big", service_name="imcodex", personality="friendly")

    assert result["thread"]["id"] == "thr_big"
    assert result["thread"]["preview"] == huge_preview


@pytest.mark.asyncio
async def test_resume_thread_trims_history_to_recent_turns() -> None:
    turns = [
        {
            "id": f"turn_{index}",
            "items": [
                {
                    "type": "userMessage",
                    "id": f"item_{index}",
                    "content": [{"type": "text", "text": f"message {index} " + ("x" * 2048)}],
                }
            ],
            "status": "completed",
        }
        for index in range(8)
    ]
    process = LimitFailingProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_recent",
                            "cwd": "D:/repo/app",
                            "preview": "seed",
                            "status": "idle",
                            "turns": turns,
                        }
                    },
                }
            ],
        },
        line_limit=1024,
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.resume_thread(thread_id="thr_recent", service_name="imcodex", personality="friendly")

    assert [turn["id"] for turn in result["thread"]["turns"]] == [
        "turn_4",
        "turn_5",
        "turn_6",
        "turn_7",
    ]


@pytest.mark.asyncio
async def test_default_spawn_pipes_stderr_for_diagnostics(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    supervisor = AppServerSupervisor(codex_bin="codex")

    await supervisor._default_spawn("codex", "app-server", "--listen", "stdio://")

    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_default_spawn_uses_larger_stream_limit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    supervisor = AppServerSupervisor(codex_bin="codex")

    await supervisor._default_spawn("codex", "app-server", "--listen", "stdio://")

    assert captured["kwargs"]["limit"] >= 1024 * 1024


@pytest.mark.asyncio
async def test_supervisor_stop_tolerates_already_exited_process() -> None:
    class AlreadyExitedProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminate_called = False
            self.wait_called = False

        def terminate(self) -> None:
            self.terminate_called = True
            raise ProcessLookupError()

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = 0
            return 0

    process = AlreadyExitedProcess()
    supervisor = AppServerSupervisor()
    supervisor._process = process
    supervisor._connection_mode = "spawned-stdio"

    await supervisor.stop()

    assert process.terminate_called is True
    assert process.wait_called is True
    assert supervisor.process is None
    assert supervisor.connection_mode == "disconnected"


@pytest.mark.asyncio
async def test_client_uses_explicit_websocket_app_server_before_spawning() -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    spawned = False

    async def unexpected_spawn(*args):
        nonlocal spawned
        spawned = True
        raise AssertionError("stdio spawn should not be used when websocket connection succeeds")

    captured_urls: list[str] = []

    async def websocket_factory(url: str):
        captured_urls.append(url)
        return websocket

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        app_server_url="ws://127.0.0.1:9999",
        spawn_process=unexpected_spawn,
        websocket_factory=websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert captured_urls == ["ws://127.0.0.1:9999"]
    assert spawned is False
    assert client.connection_mode == "external"
    assert websocket.sent[0]["method"] == "initialize"
    assert websocket.sent[0]["params"]["capabilities"]["optOutNotificationMethods"] == list(
        DEFAULT_OPT_OUT_NOTIFICATION_METHODS
    )
    await client.close()


@pytest.mark.asyncio
async def test_client_emits_observability_events_for_shared_websocket_connection(monkeypatch) -> None:
    observed_events: list[dict] = []
    observed_health: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    def capture_health(**payload) -> None:
        observed_health.append(payload)

    monkeypatch.setattr("imcodex.appserver.client.emit_event", capture_event)
    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", capture_health)

    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        app_server_url="ws://127.0.0.1:9999",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.list_threads()

    event_names = [event["event"] for event in observed_events]
    assert event_names[:2] == [
        "appserver.connect.started",
        "appserver.connect.websocket_succeeded",
    ]
    assert event_names.count("appserver.protocol.sent") == 3
    assert event_names.count("appserver.protocol.received") == 2
    sent = [event for event in observed_events if event["event"] == "appserver.protocol.sent"]
    received = [event for event in observed_events if event["event"] == "appserver.protocol.received"]
    assert sent[0]["data"]["method"] == "initialize"
    assert received[-1]["data"]["response_id"] == 2
    assert received[-1]["data"]["transport_shape"] == "response"
    initializing_health = next(
        payload for payload in observed_health if payload.get("status") == "initializing"
    )
    assert initializing_health == {
        "connected": True,
        "mode": "external",
        "status": "initializing",
        "error_type": None,
        "health_ok": None,
        "health_status_code": None,
        "health_error_type": None,
        "ready": False,
        "ownership": "external",
        "transport": "tcp-websocket",
        "endpoint": "ws://127.0.0.1:9999",
        "connection_epoch": 1,
        "reconnect_enabled": True,
        "local_image_paths": False,
    }
    assert observed_health[-1] == {
        "connected": True,
        "mode": "external",
        "status": "connected",
        "retry_attempt": None,
        "retry_delay_s": None,
        "error_type": None,
        "health_ok": None,
        "health_status_code": None,
        "health_error_type": None,
        "ready": True,
        "ownership": "external",
        "transport": "tcp-websocket",
        "endpoint": "ws://127.0.0.1:9999",
        "connection_epoch": 1,
        "reconnect_enabled": True,
        "local_image_paths": False,
    }
    await client.close()


@pytest.mark.asyncio
async def test_connection_facts_do_not_report_a_closed_transport_as_ready() -> None:
    websocket = ScriptedWebSocket(
        {"initialize": [{"id": 1, "result": {"ok": True}}]}
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        app_server_url="ws://127.0.0.1:9999",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    await client.initialize()

    websocket.closed = True

    assert client.connection_facts() == {
        "connected": False,
        "ready": False,
        "status": "disconnected",
        "mode": "external",
        "ownership": "external",
        "transport": "tcp-websocket",
        "endpoint": "ws://127.0.0.1:9999",
        "connection_epoch": 1,
        "reconnect_enabled": True,
        "local_image_paths": False,
    }
    await client.close()


@pytest.mark.asyncio
async def test_turn_send_boundary_rejects_local_image_after_capability_is_cleared() -> None:
    websocket = ScriptedWebSocket(
        {"initialize": [{"id": 1, "result": {"ok": True}}]}
    )
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            app_server_url="ws://127.0.0.1:8765",
            websocket_factory=lambda _url: websocket,
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        shared_filesystem_verifier=lambda: True,
    )
    await client.initialize()
    assert client.supports_local_image_paths() is True
    expected_epoch = client.local_image_paths_epoch()
    assert expected_epoch == 1

    client._verified_shared_filesystem = False
    client._verified_shared_filesystem_epoch = None

    with pytest.raises(AppServerError, match="cannot read bridge-local image paths"):
        await client.start_turn(
            "thr_1",
            input_items=[{"type": "localImage", "path": r"D:\media\image.png"}],
            expected_local_image_epoch=expected_epoch,
        )
    assert [payload["method"] for payload in websocket.sent] == [
        "initialize",
        "initialized",
    ]
    await client.close()


@pytest.mark.asyncio
async def test_turn_send_boundary_rejects_image_prepared_under_an_older_epoch() -> None:
    websocket = ScriptedWebSocket(
        {"initialize": [{"id": 1, "result": {"ok": True}}]}
    )
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            app_server_url="ws://127.0.0.1:8765",
            websocket_factory=lambda _url: websocket,
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        shared_filesystem_verifier=lambda: True,
    )
    await client.initialize()
    prepared_epoch = client.local_image_paths_epoch()
    assert prepared_epoch == 1

    client.connection_epoch = 2
    client._verified_shared_filesystem = True
    client._verified_shared_filesystem_epoch = 2

    with pytest.raises(AppServerError, match="for this connection"):
        await client.start_turn(
            "thr_1",
            input_items=[{"type": "localImage", "path": r"D:\media\image.png"}],
            expected_local_image_epoch=prepared_epoch,
        )
    assert [payload["method"] for payload in websocket.sent] == [
        "initialize",
        "initialized",
    ]
    await client.close()


def test_app_server_target_rejects_embedded_url_credentials() -> None:
    with pytest.raises(ValueError, match="AUTH_TOKEN"):
        AppServerSupervisor(
            codex_bin="codex",
            app_server_url="wss://bridge-user:bridge-secret@example.com/rpc?token=secret#debug",
        )


@pytest.mark.asyncio
async def test_legacy_dedicated_mode_reports_canonical_external_connection(monkeypatch) -> None:
    observed_events: list[dict] = []
    observed_health: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    def capture_health(**payload) -> None:
        observed_health.append(payload)

    monkeypatch.setattr("imcodex.appserver.client.emit_event", capture_event)
    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", capture_health)

    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    captured_urls: list[str] = []

    async def websocket_factory(url: str):
        captured_urls.append(url)
        return websocket

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.list_threads()

    assert captured_urls == ["ws://127.0.0.1:9001"]
    assert client.connection_mode == "external"
    event_names = [event["event"] for event in observed_events]
    assert event_names[:2] == [
        "appserver.connect.started",
        "appserver.connect.websocket_succeeded",
    ]
    assert observed_health[-1] == {
        "connected": True,
        "mode": "external",
        "status": "connected",
        "retry_attempt": None,
        "retry_delay_s": None,
        "error_type": None,
        "health_ok": None,
        "health_status_code": None,
        "health_error_type": None,
        "ready": True,
        "ownership": "external",
        "transport": "tcp-websocket",
        "endpoint": "ws://127.0.0.1:9001",
        "connection_epoch": 1,
        "reconnect_enabled": True,
        "local_image_paths": False,
    }
    await client.close()


@pytest.mark.asyncio
async def test_client_logs_reasoning_and_server_request_protocol_messages(monkeypatch) -> None:
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.appserver.client.emit_event", capture_event)
    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", lambda **_payload: None)

    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/start": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": "D:/repo/app",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                },
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "itemId": "item_reasoning",
                        "summaryIndex": 0,
                        "delta": "thinking through the repository state",
                    },
                },
                {
                    "id": 99,
                    "method": "item/permissions/requestApproval",
                    "params": {
                        "requestId": "native-request-abcdef",
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "itemId": "item_perm",
                        "reason": "Need broader access",
                        "permissions": {"network": {"enabled": True}},
                    },
                },
            ],
        }
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    captured_requests: list[dict] = []
    client.add_server_request_handler(captured_requests.append)

    await client.start_thread(cwd="D:/repo/app")
    await asyncio.sleep(0)

    received = [event for event in observed_events if event["event"] == "appserver.protocol.received"]
    reasoning = next(event for event in received if event["data"].get("method") == "item/reasoning/summaryTextDelta")
    approval = next(event for event in received if event["data"].get("method") == "item/permissions/requestApproval")

    assert reasoning["data"]["kind"] == "reasoning_summary_text_delta"
    assert reasoning["data"]["delta_preview"] == "thinking through the repository state"
    assert approval["data"]["kind"] == "approval_request"
    assert approval["data"]["request_id"] == "native-request-abcdef"
    assert captured_requests[0]["method"] == "item/permissions/requestApproval"
    await client.close()


def test_unknown_protocol_summary_does_not_serialize_payload_content() -> None:
    long_sensitive_key = "secret-key-" + ("k" * 400)
    extra_keys = {f"extra-{index}": index for index in range(30)}
    summary = summarize_transport_message(
        {
            "method": "plugin/newThing",
            "params": {
                "command": "run-secret-command",
                "cwd": r"D:\secret\workspace",
                "delta": "streaming-secret-delta",
                "itemId": "item_secret",
                "message": "secret message body",
                "requestId": "request_secret",
                "threadId": "thread_secret",
                "turnId": "turn_secret",
                long_sensitive_key: "token-" + ("x" * 500),
                "nested": {"raw": "payload"},
                "items": [1, 2, 3],
                **extra_keys,
            },
        }
    )

    assert summary["kind"] == "unknown"
    assert summary["payload_key_count"] == 41
    assert summary["payload_keys_sampled"] == 20
    assert summary["payload_keys_omitted"] == 21
    assert len(summary["payload_key_fingerprints"]) == 20
    assert all(set(item) == {"key_sha256", "key_length", "value_type"} for item in summary["payload_key_fingerprints"])
    assert "payload_preview" not in summary
    assert "payload_keys" not in summary
    assert "payload_value_types" not in summary
    assert "thread_id" not in summary
    assert "turn_id" not in summary
    assert "item_id" not in summary
    assert "request_id" not in summary
    assert "command" not in summary
    assert "cwd" not in summary
    assert "delta_preview" not in summary
    assert "message_preview" not in summary
    assert "token-" not in str(summary)
    assert "thread_secret" not in str(summary)
    assert "turn_secret" not in str(summary)
    assert "item_secret" not in str(summary)
    assert "request_secret" not in str(summary)
    assert long_sensitive_key not in str(summary)
    assert "streaming-secret-delta" not in str(summary)
    assert "secret message body" not in str(summary)


def test_protocol_error_summary_redacts_managed_media_path() -> None:
    local_path = "/private/user/.imcodex/channels/qq/inbound-media/abc123.png"

    summary = summarize_transport_message(
        {
            "id": 7,
            "error": {
                "code": -32603,
                "message": f"could not read local image {local_path}",
            },
        }
    )

    assert summary["has_error"] is True
    assert summary["error_code"] == -32603
    assert summary["error_message"] == "[redacted managed inbound media path]"
    assert local_path not in repr(summary)


@pytest.mark.parametrize(
    "local_path",
    [
        "/private/user/.imcodex/channels/qq/inbound-media",
        "/private/user/.imcodex/channels/qq/inbound-media/abc123.png",
        "/private/user/.imcodex/channels/telegram/inbound-media/abc123.png",
        "/private/user/.imcodex/channels/feishu/inbound-media/abc123.png",
        "/private/user/.imcodex/channels/weixin/inbound-media/abc123.png",
        "/private/user/.imcodex/channels/webhook/inbound-media/abc123.png",
        "/private/custom-weixin-state/inbound-media/abc123.png",
        r"C:\Users\owner\.imcodex\channels\qq\inbound-media\abc123.png",
        r"D:\private\custom-weixin-state\inbound-media\abc123.png",
    ],
)
def test_text_summary_redacts_managed_media_root_and_descendants(local_path: str) -> None:
    summary = summarize_text(f"could not open {local_path}")

    assert summary["text_preview"] == "[redacted managed inbound media path]"
    assert local_path not in repr(summary)


def test_text_summary_does_not_redact_similar_non_spool_path() -> None:
    value = "/private/.imcodex/channels/qq/inbound-media-archive/image.png"

    summary = summarize_text(value)

    assert summary["text_preview"] == value


def test_protocol_summary_redacts_managed_media_path_from_every_text_and_path_field() -> None:
    local_path = "/private/user/.imcodex/channels/qq/inbound-media/abc123.png"

    summary = summarize_transport_message(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "delta": f"reading {local_path}",
                "message": f"failed at {local_path}",
                "command": f"inspect {local_path}",
                "cwd": local_path,
                "tool": local_path,
                "server": local_path,
                "questions": [
                    {
                        "id": local_path,
                        "header": local_path,
                        "question": f"Use {local_path}?",
                    }
                ],
                "item": {
                    "type": "commandExecution",
                    "command": f"inspect {local_path}",
                    "cwd": local_path,
                    "tool": local_path,
                    "server": local_path,
                    "changes": [{"path": local_path}],
                },
            },
        }
    )

    assert local_path not in repr(summary)
    assert "[redacted managed inbound media path]" in repr(summary)


@pytest.mark.asyncio
async def test_stderr_diagnostics_emit_bounded_summary(monkeypatch) -> None:
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.appserver.client.emit_event", capture_event)
    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", lambda **_payload: None)

    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.connect()
    secret_line = "credential=" + ("s" * 500)
    process.stderr.lines.put_nowait((secret_line + "\n").encode("utf-8"))
    await asyncio.sleep(0)

    stderr_events = [event for event in observed_events if event["event"] == "appserver.stderr.line"]
    assert stderr_events
    data = stderr_events[-1]["data"]
    assert data["text_length"] == len(secret_line)
    assert len(data["text_sha256"]) == 64
    assert len(data["text_preview"]) <= 240
    assert "text" not in data
    assert secret_line not in str(data)
    await client.close()


@pytest.mark.asyncio
async def test_stderr_diagnostics_redact_managed_media_path(monkeypatch) -> None:
    observed_events: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.emit_event",
        lambda **payload: observed_events.append(payload),
    )
    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", lambda **_payload: None)
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    client = AppServerClient(
        supervisor=AppServerSupervisor(
            codex_bin="codex",
            core_mode="spawned-stdio",
            spawn_process=lambda *args: process,
        ),
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    local_path = r"C:\private\.imcodex\channels\qq\inbound-media\abc123.png"

    await client.connect()
    process.stderr.lines.put_nowait(f"could not read {local_path}\n".encode())
    await asyncio.sleep(0)

    event = next(item for item in observed_events if item["event"] == "appserver.stderr.line")
    assert event["data"]["text_preview"] == "[redacted managed inbound media path]"
    assert local_path not in repr(event)
    await client.close()


def test_supervisor_builds_authorization_header_from_token_file(tmp_path) -> None:
    token_file = tmp_path / "appserver.token"
    token_file.write_text(" file-token \n", encoding="utf-8")

    supervisor = AppServerSupervisor(app_server_auth_token_file=token_file)

    assert supervisor.websocket_headers() == {"Authorization": "Bearer file-token"}


def test_supervisor_prefers_direct_authorization_token_over_file(tmp_path) -> None:
    token_file = tmp_path / "appserver.token"
    token_file.write_text(" file-token \n", encoding="utf-8")

    supervisor = AppServerSupervisor(
        app_server_auth_token=" env-token ",
        app_server_auth_token_file=token_file,
    )

    assert supervisor.websocket_headers() == {"Authorization": "Bearer env-token"}


@pytest.mark.asyncio
async def test_client_passes_authorization_header_to_websocket_factory() -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    captured: dict[str, object] = {}

    async def websocket_factory(url: str, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("additional_headers")
        return websocket

    supervisor = AppServerSupervisor(
        app_server_url="ws://127.0.0.1:9999",
        app_server_auth_token="secret-token",
        websocket_factory=websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.list_threads()

    assert captured == {
        "url": "ws://127.0.0.1:9999",
        "headers": {"Authorization": "Bearer secret-token"},
    }
    await client.close()


def test_health_probe_urls_are_derived_from_ws_and_http_endpoints() -> None:
    assert derive_health_probe_urls("ws://127.0.0.1:8765") == [
        "http://127.0.0.1:8765/readyz",
        "http://127.0.0.1:8765/healthz",
    ]
    assert derive_health_probe_urls("wss://core.example.test/socket?token=secret") == [
        "https://core.example.test/readyz",
        "https://core.example.test/healthz",
    ]
    assert derive_health_probe_urls("https://core.example.test/app") == [
        "https://core.example.test/readyz",
        "https://core.example.test/healthz",
    ]
    assert derive_health_probe_urls("unix:///tmp/app-server-control.sock") == []


def test_unix_socket_endpoint_resolves_native_default_and_absolute_custom_path(
    monkeypatch,
    tmp_path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    custom = tmp_path / "control.sock"

    assert default_app_server_control_socket() == (
        codex_home / "app-server-control" / "app-server-control.sock"
    )
    assert resolve_unix_socket_path("unix://") == default_app_server_control_socket()
    assert resolve_unix_socket_path(f"unix://{custom.as_posix()}") == custom


def test_unix_socket_endpoint_preserves_native_raw_path_semantics(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert resolve_unix_socket_path("unix://relative.sock") == tmp_path / "relative.sock"
    assert resolve_unix_socket_path("unix://control%20socket.sock") == (
        tmp_path / "control%20socket.sock"
    )
    assert resolve_unix_socket_path("unix://control.sock?raw#path") == (
        tmp_path / "control.sock?raw#path"
    )
    with pytest.raises(ValueError, match="not a unix app-server endpoint"):
        resolve_unix_socket_path("UNIX:///tmp/control.sock")


def test_default_unix_socket_requires_an_existing_configured_codex_home(tmp_path) -> None:
    missing = tmp_path / "missing-codex-home"

    with pytest.raises(FileNotFoundError, match="CODEX_HOME does not exist"):
        default_app_server_control_socket(missing)

    not_a_directory = tmp_path / "codex-home-file"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="CODEX_HOME is not a directory"):
        default_app_server_control_socket(not_a_directory)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
async def test_default_unix_connector_performs_a_real_websocket_upgrade(tmp_path) -> None:
    socket_path = tmp_path / "app-server-control.sock"

    async def handler(websocket) -> None:
        await websocket.wait_closed()

    async with websockets.unix_serve(handler, str(socket_path)):
        supervisor = AppServerSupervisor(
            core_mode="shared-ws",
            app_server_url=f"unix://{socket_path.as_posix()}",
        )
        connection = await supervisor.connect_external()

        assert connection is not None
        assert supervisor.connection_mode == "external"
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
async def test_unix_connector_accepts_thread_resume_frames_larger_than_16_mib(tmp_path) -> None:
    socket_path = Path("/tmp") / f"imcodex-large-{os.getpid()}-{tmp_path.name[-8:]}.sock"
    large_preview = "x" * (17 * 1024 * 1024)

    async def handler(websocket) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            method = request.get("method")
            if method == "initialize":
                await websocket.send(json.dumps({"id": request["id"], "result": {"ok": True}}))
            elif method == "thread/resume":
                await websocket.send(
                    json.dumps(
                        {
                            "id": request["id"],
                            "result": {
                                "thread": {
                                    "id": "thr_large",
                                    "status": "idle",
                                    "preview": large_preview,
                                    "turns": [],
                                }
                            },
                        }
                    )
                )

    async with websockets.unix_serve(handler, str(socket_path)):
        client = AppServerClient(
            supervisor=AppServerSupervisor(
                app_server_url=f"unix://{socket_path.as_posix()}",
            ),
            client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
            request_timeout_s=3.0,
        )

        result = await client.resume_thread(thread_id="thr_large")

        assert len(result["thread"]["preview"]) == len(large_preview)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
async def test_unix_connect_failure_does_not_run_an_http_health_probe(tmp_path) -> None:
    health_probe_called = False

    async def unix_websocket_factory(_path: str, **_kwargs):
        raise FileNotFoundError("control socket unavailable")

    async def health_probe(_urls, _headers, _timeout_s):
        nonlocal health_probe_called
        health_probe_called = True
        raise AssertionError("Unix sockets do not expose HTTP health endpoints")

    supervisor = AppServerSupervisor(
        core_mode="shared-ws",
        app_server_url=f"unix://{(tmp_path / 'control.sock').as_posix()}",
        unix_websocket_factory=unix_websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        health_probe=health_probe,
    )

    assert await supervisor.connect_external() is None
    assert health_probe_called is False


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
@pytest.mark.parametrize("core_mode", ["dedicated-ws", "shared-ws"])
async def test_legacy_websocket_modes_collapse_to_external_on_unix(
    core_mode: str,
    tmp_path,
) -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"result": {"ok": True}}],
            "thread/list": [{"result": {"threads": []}}],
        }
    )
    socket_path = tmp_path / "app-server-control.sock"
    endpoint = f"unix://{socket_path.as_posix()}"
    captured: dict[str, object] = {}

    async def unix_websocket_factory(path: str, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return websocket

    supervisor = AppServerSupervisor(
        core_mode=core_mode,
        core_url=endpoint,
        app_server_url=endpoint,
        app_server_auth_token="local-token",
        websocket_open_timeout_s=0.75,
        unix_websocket_factory=unix_websocket_factory,
        websocket_factory=lambda _url: (_ for _ in ()).throw(
            AssertionError("TCP websocket connector must not be used for unix endpoints")
        ),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    assert await client.list_threads() == {"threads": []}
    assert captured == {
        "path": str(socket_path),
        "uri": DEFAULT_UNIX_WEBSOCKET_URI,
        "compression": None,
        "max_size": WS_MAX_SIZE,
        "open_timeout": 0.75,
        "additional_headers": {"Authorization": "Bearer local-token"},
    }
    assert client.connection_mode == "external"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
async def test_persistent_unix_websocket_reconnects_through_the_same_socket(tmp_path) -> None:
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    sockets = iter([first, second])
    connected_paths: list[str] = []
    ready_epochs: list[int] = []
    socket_path = tmp_path / "app-server-control.sock"

    async def unix_websocket_factory(path: str, **_kwargs):
        connected_paths.append(path)
        return next(sockets)

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url=f"unix://{socket_path.as_posix()}",
        unix_websocket_factory=unix_websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        sleep=lambda _delay_s: None,
    )
    client.add_connection_ready_handler(lambda epoch: ready_epochs.append(epoch))

    await client.initialize()
    first_listener = client._listener_task
    assert first_listener is not None
    first.messages.put_nowait(ConnectionError("control socket restarted"))
    await asyncio.wait_for(first_listener, timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)

    assert connected_paths == [str(socket_path), str(socket_path)]
    assert ready_epochs == [1, 2]
    assert client.connection_epoch == 2
    assert client.connection_mode == "external"
    assert [payload["method"] for payload in second.sent] == ["initialize", "initialized"]
    await client.close()


@pytest.mark.asyncio
async def test_unix_endpoint_fails_explicitly_on_native_windows(monkeypatch) -> None:
    unix_attempted = False
    spawned = False

    async def unix_websocket_factory(_path: str, **_kwargs):
        nonlocal unix_attempted
        unix_attempted = True
        raise AssertionError("unsupported platform must fail before opening unix socket")

    async def spawn_process(*_args):
        nonlocal spawned
        spawned = True
        raise AssertionError("dedicated mode must not fall back to stdio")

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="unix:///tmp/app-server-control.sock",
        unix_websocket_factory=unix_websocket_factory,
        spawn_process=spawn_process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    monkeypatch.setattr("imcodex.appserver.supervisor.os.name", "nt")

    with pytest.raises(AppServerError, match="not supported on native Windows") as exc_info:
        await client.list_threads()

    assert isinstance(exc_info.value.__cause__, UnsupportedUnixSocketError)
    assert unix_attempted is False
    assert spawned is False


@pytest.mark.asyncio
async def test_dedicated_ws_unavailable_probes_health_without_real_network() -> None:
    health_calls: list[tuple[list[str], dict[str, str], float]] = []

    async def websocket_factory(_url: str):
        raise OSError("connection refused")

    async def health_probe(urls: list[str], headers: dict[str, str], timeout_s: float) -> HealthProbeResult:
        health_calls.append((urls, headers, timeout_s))
        return HealthProbeResult(ok=False, url=urls[0], status_code=503)

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        app_server_auth_token="health-token",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        health_probe=health_probe,
        health_probe_timeout_s=0.4,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    with pytest.raises(AppServerError, match="health_status=503"):
        await client.list_threads()

    assert health_calls == [
        (
            ["http://127.0.0.1:9001/readyz", "http://127.0.0.1:9001/healthz"],
            {"Authorization": "Bearer health-token"},
            0.4,
        )
    ]


@pytest.mark.asyncio
async def test_websocket_connection_retries_with_bounded_backoff() -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    attempts = 0
    sleeps: list[float] = []

    async def websocket_factory(_url: str):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("connection refused")
        return websocket

    async def sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    supervisor = AppServerSupervisor(
        app_server_url="ws://127.0.0.1:9999",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=3, initial_delay_s=0.5, max_delay_s=2.0, jitter_fraction=0.0),
        sleep=sleep,
        random_float=lambda: 0.0,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert attempts == 3
    assert sleeps == [0.5, 1.0]
    await client.close()


def test_retry_backoff_clamps_large_attempts_and_jitters_below_the_cap() -> None:
    retry = RetryBackoff(initial_delay_s=0.5, max_delay_s=30.0, jitter_fraction=0.25)

    assert retry.delay_after_failure(10_000, random_float=lambda: 0.0, downward_jitter=True) == 30.0
    assert retry.delay_after_failure(10_000, random_float=lambda: 1.0, downward_jitter=True) == 22.5


@pytest.mark.asyncio
async def test_client_retries_native_overload_with_bounded_backoff() -> None:
    process = OverloadThenOkProcess()
    sleeps: list[float] = []

    async def sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        request_retry_policy=RetryBackoff(max_attempts=2, initial_delay_s=0.25, max_delay_s=1.0, jitter_fraction=0.0),
        sleep=sleep,
        random_float=lambda: 0.0,
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert process.thread_list_attempts == 2
    assert sleeps == [0.25]
    await client.close()


@pytest.mark.asyncio
async def test_overload_retry_does_not_replay_a_request_on_a_new_connection_epoch() -> None:
    first = ScriptedWebSocket(
        {
            "initialize": [{"result": {"ok": True}}],
            "thread/list": [
                {"error": {"code": -32001, "message": "Server overloaded; retry later"}}
            ],
        }
    )
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    sockets = iter([first, second])
    first_listener: asyncio.Task[None] | None = None

    async def sleep(_delay_s: float) -> None:
        assert first_listener is not None
        first.messages.put_nowait(ConnectionError("socket closed during overload backoff"))
        await asyncio.wait_for(first_listener, timeout=1)
        reconnect_task = client._reconnect_task
        if reconnect_task is not None:
            await asyncio.wait_for(reconnect_task, timeout=1)

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: next(sockets),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        request_retry_policy=RetryBackoff(
            max_attempts=2,
            initial_delay_s=0.25,
            max_delay_s=1.0,
            jitter_fraction=0.0,
        ),
        sleep=sleep,
    )
    await client.initialize()
    first_listener = client._listener_task

    with pytest.raises(AppServerError, match="retry was cancelled because.*connection changed"):
        await client.list_threads()

    assert client.connection_epoch == 2
    assert [payload["method"] for payload in second.sent] == ["initialize", "initialized"]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are not available on native Windows")
async def test_client_defaults_to_the_native_unix_app_server(monkeypatch, tmp_path) -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    captured_paths: list[str] = []

    async def unix_websocket_factory(path: str, **_kwargs):
        captured_paths.append(path)
        return websocket

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        unix_websocket_factory=unix_websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert captured_paths == [
        str(codex_home / "app-server-control" / "app-server-control.sock")
    ]
    assert client.connection_mode == "external"
    await client.close()


@pytest.mark.asyncio
async def test_external_connection_failure_never_falls_back_to_stdio() -> None:
    captured_urls: list[str] = []
    spawned = False

    async def websocket_factory(url: str):
        captured_urls.append(url)
        raise OSError("connection refused")

    async def spawn_process(*_args):
        nonlocal spawned
        spawned = True
        raise AssertionError("external connection failure must not spawn another App Server")

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        app_server_url="ws://127.0.0.1:8765",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        spawn_process=spawn_process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    with pytest.raises(AppServerError, match="external app-server"):
        await client.list_threads()

    assert captured_urls == ["ws://127.0.0.1:8765"]
    assert spawned is False
    await client.close()


@pytest.mark.asyncio
async def test_reset_connection_preserves_reconnected_transport_state(monkeypatch) -> None:
    observed_health: list[dict] = []

    def capture_health(**payload) -> None:
        observed_health.append(payload)

    monkeypatch.setattr("imcodex.appserver.client.mark_appserver_health", capture_health)

    first = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    second = ScriptedProcess(
        {
            "initialize": [{"id": 2, "result": {"ok": True}}],
            "thread/list": [{"id": 3, "result": {"threads": []}}],
        }
    )
    processes = iter([first, second])
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: next(processes),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    await client.connect()

    async def reconnect_on_reset(epoch: int) -> None:
        assert epoch == 1
        await client.list_threads()

    client.add_connection_reset_handler(reconnect_on_reset)
    await client._reset_connection()

    assert client.connection_mode == "spawned-stdio"
    assert client.initialized is True
    assert observed_health[-1] == {
        "connected": True,
        "mode": "spawned-stdio",
        "status": "connected",
        "retry_attempt": None,
        "retry_delay_s": None,
        "error_type": None,
        "health_ok": None,
        "health_status_code": None,
        "health_error_type": None,
        "ready": True,
        "ownership": "bridge-child",
        "transport": "stdio-jsonl",
        "endpoint": "stdio://",
        "connection_epoch": 2,
        "reconnect_enabled": False,
        "local_image_paths": True,
    }


@pytest.mark.asyncio
async def test_idle_reader_disconnect_notifies_reset_handlers() -> None:
    process = ScriptedProcess({})
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    reset_epochs: list[int] = []
    client.add_connection_reset_handler(lambda epoch: reset_epochs.append(epoch))

    await client.connect()
    listener_task = client._listener_task
    assert listener_task is not None

    process.stdout.lines.put_nowait(b"")
    await asyncio.wait_for(listener_task, timeout=1)

    assert reset_epochs == [1]
    assert client.connection_mode == "disconnected"
    assert client._listener_task is None
    assert client._reconnect_task is None
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("core_mode", ["dedicated-ws", "shared-ws"])
async def test_persistent_websocket_reconnects_and_reinitializes_without_inbound_work(
    core_mode: str,
    monkeypatch,
) -> None:
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    websocket_attempts = 0
    sleeps: list[float] = []
    ready_epochs: list[int] = []
    reset_epochs: list[int] = []
    shared_filesystem_verifications = 0
    observed_health: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.mark_appserver_health",
        lambda **payload: observed_health.append(payload),
    )

    async def websocket_factory(_url: str):
        nonlocal websocket_attempts
        websocket_attempts += 1
        if websocket_attempts == 1:
            return first
        if websocket_attempts == 2:
            raise OSError("app-server is restarting")
        return second

    async def health_probe(_urls, _headers, _timeout_s) -> HealthProbeResult:
        return HealthProbeResult(ok=False, error_type="ConnectionRefusedError")

    async def sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    def verify_shared_filesystem() -> bool:
        nonlocal shared_filesystem_verifications
        shared_filesystem_verifications += 1
        return shared_filesystem_verifications == 1

    supervisor = AppServerSupervisor(
        core_mode=core_mode,
        core_url="ws://127.0.0.1:9001",
        app_server_url="ws://127.0.0.1:9001",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        health_probe=health_probe,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        reconnect_retry_policy=RetryBackoff(
            initial_delay_s=0.5,
            max_delay_s=30.0,
            jitter_fraction=0.0,
        ),
        sleep=sleep,
        random_float=lambda: 0.0,
        shared_filesystem_verifier=verify_shared_filesystem,
    )
    client.add_connection_ready_handler(lambda epoch: ready_epochs.append(epoch))
    client.add_connection_reset_handler(lambda epoch: reset_epochs.append(epoch))

    await client.initialize()
    assert client.supports_local_image_paths() is True
    first_listener = client._listener_task
    assert first_listener is not None

    first.messages.put_nowait(ConnectionError("socket closed"))
    await asyncio.wait_for(first_listener, timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)

    assert websocket_attempts == 3
    assert sleeps == [0.5]
    assert reset_epochs == [1]
    assert ready_epochs == [1, 2]
    assert client.connection_epoch == 2
    assert client.connection_mode == "external"
    assert client.initialized is True
    assert shared_filesystem_verifications == 2
    assert client.supports_local_image_paths() is False
    assert [payload["method"] for payload in second.sent] == ["initialize", "initialized"]
    statuses = [payload.get("status") for payload in observed_health]
    assert "reconnecting" in statuses
    last_initializing = len(statuses) - 1 - statuses[::-1].index("initializing")
    assert "connected" in statuses[last_initializing + 1 :]
    assert statuses[-1] == "connected"
    assert any(
        payload.get("retry_attempt") == 2 and payload.get("retry_delay_s") == 0.5
        for payload in observed_health
    )
    assert observed_health[-1] == {
        "connected": True,
        "mode": "external",
        "status": "connected",
        "retry_attempt": None,
        "retry_delay_s": None,
        "error_type": None,
        "health_ok": None,
        "health_status_code": None,
        "health_error_type": None,
        "ready": True,
        "ownership": "external",
        "transport": "tcp-websocket",
        "endpoint": "ws://127.0.0.1:9001",
        "connection_epoch": 2,
        "reconnect_enabled": True,
        "local_image_paths": False,
    }
    await client.close()


@pytest.mark.asyncio
async def test_close_cancels_a_sleeping_background_reconnect() -> None:
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    websocket_attempts = 0
    sleep_started = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    never_resume = asyncio.Event()

    async def websocket_factory(_url: str):
        nonlocal websocket_attempts
        websocket_attempts += 1
        if websocket_attempts == 1:
            return first
        raise OSError("app-server is offline")

    async def health_probe(_urls, _headers, _timeout_s) -> HealthProbeResult:
        return HealthProbeResult(ok=False, error_type="ConnectionRefusedError")

    async def sleep(_delay_s: float) -> None:
        sleep_started.set()
        try:
            await never_resume.wait()
        except asyncio.CancelledError:
            sleep_cancelled.set()
            raise

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        health_probe=health_probe,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        reconnect_retry_policy=RetryBackoff(initial_delay_s=0.5, max_delay_s=30.0),
        sleep=sleep,
    )

    await client.initialize()
    first.messages.put_nowait(ConnectionError("socket closed"))
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    await client.close()

    assert sleep_cancelled.is_set()
    assert client._reconnect_task is None
    assert client._transport is None


@pytest.mark.asyncio
async def test_connect_finishing_after_close_does_not_install_a_transport() -> None:
    websocket = ScriptedWebSocket({})
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def websocket_factory(_url: str):
        connect_started.set()
        await release_connect.wait()
        return websocket

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    connect_task = asyncio.create_task(client.connect())
    await asyncio.wait_for(connect_started.wait(), timeout=1)
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    release_connect.set()

    with pytest.raises(AppServerError, match="client is closed"):
        await connect_task
    await close_task

    assert websocket.closed is True
    assert client._transport is None
    assert client._listener_task is None


@pytest.mark.asyncio
async def test_initialize_serializes_callers_but_ready_owner_can_issue_requests() -> None:
    websocket = ScriptedWebSocket(
        {
            "initialize": [{"result": {"ok": True}}],
            "thread/list": [{"result": {"threads": []}}],
            "model/list": [{"result": {"models": []}}],
        }
    )
    handler_rpc_finished = asyncio.Event()
    release_handler = asyncio.Event()

    async def ready_handler(_epoch: int) -> None:
        assert await client.list_threads() == {"threads": []}
        handler_rpc_finished.set()
        await release_handler.wait()

    supervisor = AppServerSupervisor(
        app_server_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(ready_handler)

    initialize_task = asyncio.create_task(client.initialize())
    await asyncio.wait_for(handler_rpc_finished.wait(), timeout=1)
    concurrent_call = asyncio.create_task(client.list_models())
    await asyncio.sleep(0)

    assert [payload["method"] for payload in websocket.sent].count("initialize") == 1
    assert not any(payload.get("method") == "model/list" for payload in websocket.sent)

    release_handler.set()
    await initialize_task
    assert await concurrent_call == {"models": []}
    assert [payload["method"] for payload in websocket.sent].count("initialize") == 1
    await client.close()


@pytest.mark.asyncio
async def test_connection_change_during_ready_handler_fails_fast_without_false_ready() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})

    async def ready_handler(_epoch: int) -> None:
        await client._reset_connection(notify_handlers=False)
        with pytest.raises(AppServerError, match="connection changed"):
            await client.list_threads()

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(ready_handler)

    with pytest.raises(AppServerError, match="connection changed"):
        await asyncio.wait_for(client.initialize(), timeout=1)

    assert client.initialized is False
    assert client.connection_mode == "disconnected"
    await client.close()


@pytest.mark.asyncio
async def test_ready_owner_fails_while_another_task_is_resetting_the_connection() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    handler_started = asyncio.Event()
    let_handler_issue_request = asyncio.Event()
    handler_failed_fast = asyncio.Event()
    dispatcher_cancelled = asyncio.Event()
    release_dispatcher = asyncio.Event()

    async def ready_handler(_epoch: int) -> None:
        handler_started.set()
        await let_handler_issue_request.wait()
        with pytest.raises(AppServerError, match="reset during initialization"):
            await client.list_threads()
        handler_failed_fast.set()

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(ready_handler)

    initialize_task = asyncio.create_task(client.initialize())
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    original_dispatcher = client._dispatcher_task
    assert original_dispatcher is not None
    original_dispatcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original_dispatcher

    async def gated_dispatcher() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            dispatcher_cancelled.set()
            await release_dispatcher.wait()
            raise

    client._dispatcher_task = asyncio.create_task(gated_dispatcher())
    listener_task = client._listener_task
    assert listener_task is not None
    websocket.messages.put_nowait(ConnectionError("socket closed"))
    await asyncio.wait_for(dispatcher_cancelled.wait(), timeout=1)

    let_handler_issue_request.set()
    await asyncio.wait_for(handler_failed_fast.wait(), timeout=1)
    assert not any(payload.get("method") == "thread/list" for payload in websocket.sent)

    release_dispatcher.set()
    with pytest.raises(AppServerError, match="reset during initialization|connection changed"):
        await asyncio.wait_for(initialize_task, timeout=1)
    await asyncio.wait_for(listener_task, timeout=1)
    await client.close()


@pytest.mark.asyncio
async def test_throwing_reset_handler_does_not_stop_background_reconnect(monkeypatch) -> None:
    observed_events: list[dict] = []
    monkeypatch.setattr(
        "imcodex.appserver.client.emit_event",
        lambda **payload: observed_events.append(payload),
    )
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    sockets = iter([first, second])

    def failing_reset_handler(_epoch: int) -> None:
        raise RuntimeError("local cleanup failed")

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: next(sockets),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_reset_handler(failing_reset_handler)

    await client.initialize()
    listener_task = client._listener_task
    assert listener_task is not None
    first.messages.put_nowait(ConnectionError("socket closed"))
    await asyncio.wait_for(listener_task, timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)

    assert client.connection_epoch == 2
    assert client.initialized is True
    assert any(
        event["event"] == "appserver.connection_reset_handler.failed"
        for event in observed_events
    )
    await client.close()


@pytest.mark.asyncio
async def test_failed_ready_epoch_is_reset_before_the_next_reconnect_attempt() -> None:
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    third = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    sockets = iter([first, second, third])
    ready_epochs: list[int] = []
    reset_epochs: list[int] = []

    async def ready_handler(epoch: int) -> None:
        ready_epochs.append(epoch)
        if epoch == 2:
            raise RuntimeError("rehydration failed")

    async def sleep(_delay_s: float) -> None:
        return None

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: next(sockets),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
        reconnect_retry_policy=RetryBackoff(initial_delay_s=0.5, max_delay_s=30.0),
        sleep=sleep,
    )
    client.add_connection_ready_handler(ready_handler)
    client.add_connection_reset_handler(lambda epoch: reset_epochs.append(epoch))

    await client.initialize()
    listener_task = client._listener_task
    assert listener_task is not None
    first.messages.put_nowait(ConnectionError("socket closed"))
    await asyncio.wait_for(listener_task, timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)

    assert ready_epochs == [1, 2, 3]
    assert reset_epochs == [1, 2]
    assert second.closed is True
    assert client.connection_epoch == 3
    assert client.initialized is True
    await client.close()


@pytest.mark.asyncio
async def test_repeatedly_cancelling_reset_waits_for_transport_close_to_finish() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def gated_close() -> None:
        close_started.set()
        await release_close.wait()
        websocket.closed = True

    websocket.close = gated_close  # type: ignore[method-assign]
    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    await client.initialize()

    reset_task = asyncio.create_task(client._reset_connection())
    client._reconnect_task = reset_task
    await asyncio.wait_for(close_started.wait(), timeout=1)
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    reset_task.cancel()
    await asyncio.sleep(0)
    release_close.set()
    await asyncio.wait_for(close_task, timeout=1)

    assert reset_task.cancelled()
    assert websocket.closed is True
    assert client._transport is None
    assert client._listener_task is None


@pytest.mark.asyncio
async def test_initialize_rejects_cached_success_after_close_begins() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def gated_close() -> None:
        close_started.set()
        await release_close.wait()
        websocket.closed = True

    websocket.close = gated_close  # type: ignore[method-assign]
    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    await client.initialize()

    close_task = asyncio.create_task(client.close())
    await asyncio.wait_for(close_started.wait(), timeout=1)
    with pytest.raises(AppServerError, match="client is closed"):
        await client.initialize()
    release_close.set()
    await asyncio.wait_for(close_task, timeout=1)


@pytest.mark.asyncio
async def test_cancelling_ready_handlers_resets_the_half_ready_transport() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    handler_started = asyncio.Event()

    async def ready_handler(_epoch: int) -> None:
        handler_started.set()
        await asyncio.Event().wait()

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(ready_handler)

    initialize_task = asyncio.create_task(client.initialize())
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    initialize_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initialize_task

    assert websocket.closed is True
    assert client._transport is None
    assert client.initialized is False
    await client.close()


@pytest.mark.asyncio
async def test_reset_handlers_cannot_reenter_client_while_initialize_owns_its_lock() -> None:
    process = ScriptedProcess({"initialize": [{"result": {"ok": True}}]})
    handler_started = asyncio.Event()
    reset_handler_finished = asyncio.Event()
    process_starts = 0

    def spawn_process(*_args):
        nonlocal process_starts
        process_starts += 1
        return process

    async def ready_handler(_epoch: int) -> None:
        handler_started.set()
        await asyncio.Event().wait()

    async def reset_handler(_epoch: int) -> None:
        with pytest.raises(AppServerError, match="reset during initialization"):
            await client.list_threads()
        reset_handler_finished.set()

    supervisor = AppServerSupervisor(
        core_mode="spawned-stdio",
        spawn_process=spawn_process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_connection_ready_handler(ready_handler)
    client.add_connection_reset_handler(reset_handler)

    initialize_task = asyncio.create_task(client.initialize())
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    initialize_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(initialize_task, timeout=1)

    assert reset_handler_finished.is_set()
    assert process_starts == 1
    assert client._initialize_lock.locked() is False
    assert client._resetting is False
    await client.close()


@pytest.mark.asyncio
async def test_reset_handler_reentry_fails_if_another_initialize_takes_the_lock_late() -> None:
    first = ScriptedProcess({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedProcess({"initialize": [{"result": {"ok": True}}]})
    processes = iter([first, second])
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_failed_fast = asyncio.Event()

    async def reset_handler(_epoch: int) -> None:
        handler_started.set()
        await release_handler.wait()
        with pytest.raises(AppServerError, match="reset during initialization"):
            await client.list_threads()
        handler_failed_fast.set()

    supervisor = AppServerSupervisor(
        core_mode="spawned-stdio",
        spawn_process=lambda *_args: next(processes),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    await client.initialize()
    client.add_connection_reset_handler(reset_handler)

    reset_task = asyncio.create_task(client._reset_connection())
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    initialize_task = asyncio.create_task(client.initialize())
    for _ in range(20):
        if client._initialize_lock_owner_task is initialize_task:
            break
        await asyncio.sleep(0)
    assert client._initialize_lock_owner_task is initialize_task

    release_handler.set()
    await asyncio.wait_for(handler_failed_fast.wait(), timeout=1)
    await asyncio.wait_for(reset_task, timeout=1)
    await asyncio.wait_for(initialize_task, timeout=1)

    assert client.connection_epoch == 2
    assert client.initialized is True
    assert client._initialize_lock.locked() is False
    assert client._resetting is False
    await client.close()


@pytest.mark.asyncio
async def test_dispatcher_that_initiates_reset_exits_with_its_connection_epoch() -> None:
    websocket = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    reset_finished = asyncio.Event()

    async def reset_from_dispatcher(_request: dict) -> None:
        await client._reset_connection(notify_handlers=False)
        reset_finished.set()

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_server_request_handler(reset_from_dispatcher)
    await client.initialize()
    old_dispatcher = client._server_request_dispatcher_task
    assert old_dispatcher is not None

    websocket.messages.put_nowait(
        json.dumps(
            {
                "id": 91,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thr_1", "turnId": "turn_1"},
            }
        )
    )
    await asyncio.wait_for(reset_finished.wait(), timeout=1)
    await asyncio.wait_for(old_dispatcher, timeout=1)

    assert old_dispatcher.done()
    assert client._server_request_dispatcher_task is None
    await client.close()


@pytest.mark.asyncio
async def test_failed_server_reply_resets_and_reconnects_without_replaying_the_reply() -> None:
    first = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    second = ScriptedWebSocket({"initialize": [{"result": {"ok": True}}]})
    original_send = first.send
    reply_failed = asyncio.Event()
    sockets = iter([first, second])

    async def fail_response_frame(raw: str) -> None:
        payload = json.loads(raw)
        if payload.get("id") == 91 and "result" in payload:
            raise ConnectionError("socket write failed")
        await original_send(raw)

    first.send = fail_response_frame  # type: ignore[method-assign]

    async def reply_from_dispatcher(request: dict) -> None:
        try:
            await client.reply_to_transport_request(
                request["params"]["_transport_request_id"],
                {"decision": "accept"},
                expected_connection_epoch=request["params"]["_connection_epoch"],
            )
        except ConnectionError:
            reply_failed.set()

    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: next(sockets),
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    client.add_server_request_handler(reply_from_dispatcher)
    await client.initialize()
    old_dispatcher = client._server_request_dispatcher_task
    assert old_dispatcher is not None

    first.messages.put_nowait(
        json.dumps(
            {
                "id": 91,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thr_1", "turnId": "turn_1"},
            }
        )
    )
    await asyncio.wait_for(reply_failed.wait(), timeout=1)
    reconnect_task = client._reconnect_task
    if reconnect_task is not None:
        await asyncio.wait_for(reconnect_task, timeout=1)
    await asyncio.wait_for(old_dispatcher, timeout=1)

    assert client.connection_epoch == 2
    assert client.initialized is True
    assert old_dispatcher.done()
    assert not any(payload.get("id") == 91 for payload in second.sent)
    await client.close()


@pytest.mark.asyncio
async def test_old_epoch_response_and_server_reply_cannot_cross_connections() -> None:
    websocket = ScriptedWebSocket({})
    supervisor = AppServerSupervisor(
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=lambda _url: websocket,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    await client.connect()
    client.connection_epoch = 2
    future = asyncio.get_running_loop().create_future()
    client._pending_futures[77] = (2, future)

    assert client._dispatch_response({"id": 77, "result": {"from": "old"}}, 1) is True
    assert future.done() is False
    assert client._dispatch_response({"id": 77, "result": {"from": "current"}}, 2) is True
    assert await future == {"id": 77, "result": {"from": "current"}}

    with pytest.raises(AppServerError, match="expired app-server connection"):
        await client.reply_to_transport_request(
            91,
            {"decision": "accept"},
            expected_connection_epoch=1,
        )
    assert not any(payload.get("id") == 91 for payload in websocket.sent)
    client._pending_futures.pop(77, None)
    await client.close()


@pytest.mark.asyncio
async def test_dedicated_ws_mode_does_not_fallback_to_spawned_stdio() -> None:
    spawned = False

    async def websocket_factory(_url: str):
        raise OSError("connection refused")

    async def health_probe(urls: list[str], headers: dict[str, str], timeout_s: float) -> HealthProbeResult:
        del headers, timeout_s
        return HealthProbeResult(ok=False, url=urls[0], error_type="ConnectionRefusedError")

    async def unexpected_spawn(*args):
        nonlocal spawned
        spawned = True
        raise AssertionError("dedicated core mode must not spawn stdio fallback")

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:9001",
        websocket_factory=websocket_factory,
        websocket_retry_policy=RetryBackoff(max_attempts=1),
        health_probe=health_probe,
        spawn_process=unexpected_spawn,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    with pytest.raises(AppServerError, match="external app-server"):
        await client.list_threads()

    assert spawned is False

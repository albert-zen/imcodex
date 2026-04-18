from __future__ import annotations

import asyncio
import json

import pytest

from imcodex.appserver import AppServerClient, AppServerError, AppServerSupervisor


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
        self.messages: asyncio.Queue[str] = asyncio.Queue()
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
        return await self.messages.get()

    async def close(self) -> None:
        self.closed = True

    def _prepare_scripted_message(self, request: dict, scripted: dict) -> dict:
        if "method" in scripted:
            return dict(scripted)
        response = dict(scripted)
        if "id" in request:
            response["id"] = request["id"]
        return response


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
async def test_default_spawn_does_not_pipe_stderr(monkeypatch) -> None:
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
    assert "stderr" not in captured["kwargs"]


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
    assert client.connection_mode == "shared-ws"
    assert websocket.sent[0]["method"] == "initialize"
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

    assert [event["event"] for event in observed_events] == [
        "appserver.connect.started",
        "appserver.connect.shared_ws_succeeded",
    ]
    assert observed_health[-1] == {"connected": True, "mode": "shared-ws"}
    await client.close()


@pytest.mark.asyncio
async def test_client_probes_default_websocket_app_server_before_spawning() -> None:
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
        websocket_factory=websocket_factory,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert captured_urls == ["ws://127.0.0.1:8765"]
    assert client.connection_mode == "shared-ws"
    await client.close()


@pytest.mark.asyncio
async def test_client_falls_back_to_stdio_when_websocket_connection_fails() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
        }
    )
    captured_urls: list[str] = []

    async def websocket_factory(url: str):
        captured_urls.append(url)
        raise OSError("connection refused")

    supervisor = AppServerSupervisor(
        codex_bin="codex",
        websocket_factory=websocket_factory,
        spawn_process=lambda *args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert result == {"threads": []}
    assert captured_urls == ["ws://127.0.0.1:8765"]
    assert client.connection_mode == "spawned-stdio"
    assert process.sent[0]["method"] == "initialize"
    await client.close()

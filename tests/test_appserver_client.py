import asyncio
from dataclasses import dataclass
import json
import queue

import pytest

from imcodex.appserver import AppServerClient, AppServerError


@dataclass
class ScriptedWebSocket:
    sent: list
    incoming: asyncio.Queue
    scripts: dict[int, list[str]]
    closed: bool = False

    async def send(self, message: str) -> None:
        self.sent.append(message)
        payload = json.loads(message)
        request_id = payload.get("id")
        if isinstance(request_id, int):
            for item in self.scripts.get(request_id, []):
                self.incoming.put_nowait(item)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


class FlakyWebSocket(ScriptedWebSocket):
    def __init__(self, *, sent, incoming, scripts, fail_after_recvs: int) -> None:
        super().__init__(sent=sent, incoming=incoming, scripts=scripts)
        self.fail_after_recvs = fail_after_recvs
        self.recv_count = 0

    async def recv(self) -> str:
        self.recv_count += 1
        try:
            return await asyncio.wait_for(super().recv(), timeout=0.01)
        except asyncio.TimeoutError:
            if self.recv_count > self.fail_after_recvs and len(self.sent) >= 2:
                raise RuntimeError("socket dropped")
            raise


class SyncScriptedWebSocket:
    def __init__(self, *, sent, incoming, scripts) -> None:
        self.sent = sent
        self.incoming = incoming
        self.scripts = scripts
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)
        payload = json.loads(message)
        request_id = payload.get("id")
        if isinstance(request_id, int):
            for item in self.scripts.get(request_id, []):
                self.incoming.put(item)

    def recv(self) -> str:
        item = self.incoming.get(timeout=1)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_initialize_sends_initialize_and_initialized_notification():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(sent=[], incoming=incoming, scripts={})
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    response = await client.initialize()

    assert response["ok"] is True
    assert client.initialized is True
    assert len(websocket.sent) == 2
    assert '"method": "initialize"' in websocket.sent[0]
    assert '"method": "initialized"' in websocket.sent[1]
    await client.close()


@pytest.mark.asyncio
async def test_client_supports_sync_websocket_factory():
    incoming = queue.Queue()
    incoming.put('{"id":1,"result":{"ok":true}}')
    websocket = SyncScriptedWebSocket(sent=[], incoming=incoming, scripts={})
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    response = await client.initialize()

    assert response["ok"] is True
    assert len(websocket.sent) == 2
    assert websocket.closed is False
    await client.close()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_turn_request_correlation_and_notifications():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"method":"turn/started","params":{"turn":{"id":"turn_1","status":"inProgress"},"threadId":"thr_1"}}',
                '{"method":"item/agentMessage/delta","params":{"threadId":"thr_1","turnId":"turn_1","itemId":"item_1","delta":"Hello"}}',
                '{"method":"item/completed","params":{"threadId":"thr_1","turnId":"turn_1","item":{"id":"item_1","type":"agentMessage","text":"Hello world"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_1","turn":{"id":"turn_1","status":"completed"}}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    events = []
    client.add_notification_handler(events.append)
    result = await client.start_thread(cwd="D:/desktop/project")
    turn = await client.start_turn(thread_id=result["thread"]["id"], text="hello")

    assert result["thread"]["id"] == "thr_1"
    assert turn["turn"]["id"] == "turn_1"
    assert any(e["method"] == "turn/started" for e in events)
    assert any(e["method"] == "item/agentMessage/delta" for e in events)
    assert any(e["method"] == "turn/completed" for e in events)
    assert client.last_agent_message == "Hello world"
    await client.close()


@pytest.mark.asyncio
async def test_steer_turn_uses_expected_turn_id_and_text_input():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"turnId":"turn_1"}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    result = await client.steer_turn(
        thread_id="thr_1",
        turn_id="turn_1",
        text="Actually focus on failing tests first",
    )

    assert result["turnId"] == "turn_1"
    assert '"method": "turn/steer"' in websocket.sent[2]
    assert '"expectedTurnId": "turn_1"' in websocket.sent[2]
    assert '"text": "Actually focus on failing tests first"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_first_thread_request_connects_and_initializes_lazily():
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"thread":{"id":"thr_lazy"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.start_thread(cwd="D:/desktop/project")

    assert result["thread"]["id"] == "thr_lazy"
    assert '"method": "initialize"' in websocket.sent[0]
    assert '"method": "initialized"' in websocket.sent[1]
    assert '"method": "thread/start"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_resume_thread_uses_thread_resume_and_thread_id():
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"thread":{"id":"thr_existing"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.resume_thread(thread_id="thr_existing", cwd="D:/desktop/project")

    assert result["thread"]["id"] == "thr_existing"
    assert '"method": "thread/resume"' in websocket.sent[2]
    assert '"threadId": "thr_existing"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_thread_and_turn_requests_include_native_permission_fields() -> None:
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"thread":{"id":"thr_perm"}}}'],
            3: ['{"id":3,"result":{"turn":{"id":"turn_perm","status":"inProgress"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.start_thread(
        cwd="D:/desktop/project",
        approval_policy="on-request",
        sandbox_policy={"type": "workspaceWrite"},
        approvals_reviewer="user",
    )
    await client.start_turn(
        thread_id="thr_perm",
        text="hello",
        approval_policy="never",
    )

    assert '"approvalPolicy": "on-request"' in websocket.sent[2]
    assert '"sandboxPolicy": {"type": "workspaceWrite"}' in websocket.sent[2]
    assert '"approvalsReviewer": "user"' in websocket.sent[2]
    assert '"approvalPolicy": "never"' in websocket.sent[3]
    assert '"sandboxPolicy"' not in websocket.sent[3]
    await client.close()


@pytest.mark.asyncio
async def test_list_threads_uses_thread_list_method() -> None:
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"threads":[{"id":"thr_1"},{"id":"thr_2"}]}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.list_threads()

    assert [thread["id"] for thread in result["threads"]] == ["thr_1", "thr_2"]
    assert '"method": "thread/list"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_read_thread_uses_thread_read_method_and_thread_id() -> None:
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"thread":{"id":"thr_read","name":"Investigate","cwd":"D:/desktop/project"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.read_thread("thr_read")

    assert result["thread"]["id"] == "thr_read"
    assert result["thread"]["name"] == "Investigate"
    assert '"method": "thread/read"' in websocket.sent[2]
    assert '"threadId": "thr_read"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_set_thread_name_uses_thread_name_set_method() -> None:
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"thread":{"id":"thr_read","name":"Investigate alpha"}}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.set_thread_name("thr_read", "Investigate alpha")

    assert result["thread"]["name"] == "Investigate alpha"
    assert '"method": "thread/name/set"' in websocket.sent[2]
    assert '"threadId": "thr_read"' in websocket.sent[2]
    assert '"name": "Investigate alpha"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_archive_thread_uses_thread_archive_method() -> None:
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"result":{"archived":true}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    result = await client.archive_thread("thr_read")

    assert result["archived"] is True
    assert '"method": "thread/archive"' in websocket.sent[2]
    assert '"threadId": "thr_read"' in websocket.sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_error_response_raises_app_server_error_without_waiting_for_timeout():
    incoming = asyncio.Queue()
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            1: ['{"id":1,"result":{"ok":true}}'],
            2: ['{"id":2,"error":{"message":"invalid request"}}'],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
        request_timeout_s=0.01,
    )

    with pytest.raises(AppServerError, match="invalid request"):
        await client.start_thread(cwd="D:/desktop/project")

    await client.close()


@pytest.mark.asyncio
async def test_async_notification_handlers_are_awaited():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: [
                '{"id":2,"result":{"thread":{"id":"thr_1"}}}',
                '{"method":"turn/completed","params":{"threadId":"thr_1","turn":{"id":"turn_1","status":"completed"}}}',
            ]
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    seen = []

    async def capture(notification):
        seen.append(notification["method"])

    await client.connect()
    await client.initialize()
    client.add_notification_handler(capture)
    await client.start_thread(cwd="D:/desktop/project")
    await asyncio.sleep(0)

    assert seen == ["turn/completed"]
    await client.close()


@pytest.mark.asyncio
async def test_server_initiated_requests_are_buffered():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={
            2: ['{"id":2,"result":{"thread":{"id":"thr_1"}}}'],
            3: [
                '{"id":3,"result":{"turn":{"id":"turn_1","status":"inProgress"}}}',
                '{"id":99,"method":"item/tool/requestUserInput","params":{"threadId":"thr_1","turnId":"turn_1","questionId":"q1","prompt":"What time?"}}',
            ],
        },
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    captured = []
    client.add_server_request_handler(captured.append)
    await client.start_thread(cwd="D:/desktop/project")
    await client.start_turn(thread_id="thr_1", text="hello")
    await asyncio.sleep(0)

    pending = client.pending_requests()
    assert pending[0]["method"] == "item/tool/requestUserInput"
    assert captured[0]["params"]["_request_id"] == "99"
    assert pending[0]["params"]["questionId"] == "q1"
    await client.close()


@pytest.mark.asyncio
async def test_reply_to_pending_request_tracks_decision():
    client = AppServerClient(
        websocket_factory=lambda _: None,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )
    websocket = ScriptedWebSocket(sent=[], incoming=asyncio.Queue(), scripts={})
    client._ws = websocket
    client._pending_requests["ticket-1"] = {"id": 10, "method": "item/fileChange/requestApproval"}

    payload = await client.reply_to_server_request("ticket-1", {"decision": "accept"})

    assert payload["id"] == 10
    assert payload["result"] == {"decision": "accept"}
    assert "ticket-1" not in client._pending_requests
    assert '"decision": "accept"' in websocket.sent[0]


@pytest.mark.asyncio
async def test_unknown_ticket_raises():
    client = AppServerClient(
        websocket_factory=lambda _: None,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    with pytest.raises(AppServerError):
        await client.reply_to_server_request("missing", {"decision": "accept"})


@pytest.mark.asyncio
async def test_client_recovers_on_followup_request_after_receive_loop_failure():
    connections = []

    def websocket_factory(_: str):
        index = len(connections)
        if index == 0:
            incoming = queue.Queue()
            incoming.put('{"id":1,"result":{"ok":true}}')
            ws = SyncScriptedWebSocket(sent=[], incoming=incoming, scripts={})
        else:
            incoming = asyncio.Queue()
            ws = ScriptedWebSocket(
                sent=[],
                incoming=incoming,
                scripts={
                    3: ['{"id":3,"result":{"ok":true}}'],
                    4: ['{"id":4,"result":{"thread":{"id":"thr_2"}}}'],
                },
            )
        connections.append(ws)
        return ws

    client = AppServerClient(
        websocket_factory=websocket_factory,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
    )

    await client.connect()
    await client.initialize()
    connections[0].incoming.put(RuntimeError("socket dropped"))
    await asyncio.sleep(0)

    with pytest.raises(AppServerError, match="socket dropped"):
        await client.start_thread(cwd="D:/desktop/project")

    result = await client.start_thread(cwd="D:/desktop/project")

    assert result["thread"]["id"] == "thr_2"
    assert len(connections) == 2
    assert '"method": "initialize"' in connections[1].sent[0]
    assert '"method": "initialized"' in connections[1].sent[1]
    assert '"method": "thread/start"' in connections[1].sent[2]
    await client.close()


@pytest.mark.asyncio
async def test_request_timeout_resets_connection_and_raises_app_server_error():
    incoming = asyncio.Queue()
    incoming.put_nowait('{"id":1,"result":{"ok":true}}')
    websocket = ScriptedWebSocket(
        sent=[],
        incoming=incoming,
        scripts={},
    )
    client = AppServerClient(
        websocket_factory=lambda _: websocket,
        transport_url="ws://127.0.0.1:8765",
        client_info={"name": "imcodex", "title": "IM Codex", "version": "0.1.0"},
        request_timeout_s=0.01,
    )

    await client.connect()
    await client.initialize()

    with pytest.raises(AppServerError, match="thread/start timed out"):
        await client.start_thread(cwd="D:/desktop/project")

    assert websocket.closed is True
    assert client.initialized is False

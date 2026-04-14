from __future__ import annotations

import asyncio
import json

import pytest

from imcodex.appserver import AppServerClient, AppServerError, AppServerSupervisor, CodexBackend
from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


class FakeStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


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
        self.inputs: list[dict] = []
        self.closed = False
        self.returncode: int | None = None

    def on_input(self, raw: str) -> None:
        payload = json.loads(raw)
        self.inputs.append(payload)
        method = payload.get("method")
        if method == "initialized":
            return
        if method is None:
            return
        for message in self.scripts.get(method, []):
            self.stdout.lines.put_nowait((json.dumps(message) + "\n").encode("utf-8"))

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.lines.put_nowait(b"")

    async def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class CapturingSink:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def send_message(self, message: OutboundMessage) -> None:
        self.messages.append(message)


def _build_service(store: ConversationStore, process: ScriptedProcess, sink: CapturingSink):
    supervisor = AppServerSupervisor(codex_bin="codex", spawn_process=lambda *args: process)
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": "imcodex", "title": "IMCodex", "version": "0.1.0"},
    )
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name="imcodex-test"),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=sink,
    )
    client.add_notification_handler(service.handle_notification)
    client.add_server_request_handler(service.handle_server_request)
    return client, service


@pytest.mark.asyncio
async def test_text_turn_flows_from_cwd_to_final_result() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/start": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                }
            ],
            "turn/start": [
                {"id": 3, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}},
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "item": {
                            "id": "item_1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Hello from Codex",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr_1",
                        "turn": {"id": "turn_1", "status": "completed"},
                    },
                },
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="inspect the repo",
        )
    )
    await asyncio.sleep(0)

    assert [message.message_type for message in messages] == ["accepted"]
    assert sink.messages[-1].text == "Hello from Codex"
    await client.close()


@pytest.mark.asyncio
async def test_stale_resume_clears_binding_and_returns_status_message() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [{"id": 2, "error": {"message": "unknown thread"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_stale")
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="continue",
        )
    )

    assert messages[0].message_type == "status"
    assert "Use /new or /thread attach <thread-id>" in messages[0].text
    assert store.get_binding("qq", "conv-1").thread_id is None


@pytest.mark.asyncio
async def test_in_flight_turn_uses_native_steer() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/steer": [{"id": 2, "result": {"turnId": "turn_1"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="one more thing",
        )
    )

    assert messages[0].message_type == "accepted"
    await client.close()


@pytest.mark.asyncio
async def test_stale_in_flight_turn_falls_back_to_native_turn_start() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/steer": [{"id": 2, "error": {"message": "no active turn"}}],
            "thread/resume": [
                {
                    "id": 3,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                }
            ],
            "turn/start": [{"id": 4, "result": {"turn": {"id": "turn_2", "status": "inProgress"}}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="fresh turn please",
        )
    )

    assert messages[0].message_type == "accepted"
    assert store.get_active_turn("thr_1") == ("turn_2", "inProgress")
    methods = [payload.get("method") for payload in process.inputs if payload.get("method")]
    assert methods.count("turn/steer") == 1
    assert methods.count("turn/start") == 1
    await client.close()


@pytest.mark.asyncio
async def test_threads_command_returns_status_message_when_backend_list_fails() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "error": {"message": "server overloaded"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/threads",
        )
    )

    assert messages[0].message_type == "status"
    assert "could not be refreshed from Codex" in messages[0].text


@pytest.mark.asyncio
async def test_expired_request_reply_returns_status_and_clears_route() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve native-request-abcdef",
        )
    )

    assert messages[0].message_type == "status"
    assert "is no longer pending" in messages[0].text
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is None
    await client.close()


@pytest.mark.asyncio
async def test_transient_request_reply_failure_keeps_route_for_retry() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    async def broken_reply(request_id: str, payload: dict) -> None:
        raise AppServerError(f"broken pipe while replying to {request_id}")

    service.backend.reply_to_server_request = broken_reply  # type: ignore[method-assign]

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve native-request-abcdef",
        )
    )

    assert messages[0].message_type == "status"
    assert "could not be sent to Codex right now" in messages[0].text
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is not None
    await client.close()


@pytest.mark.asyncio
async def test_status_command_returns_status_message_when_backend_read_fails() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/read": [{"id": 2, "error": {"message": "server overloaded"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_thread_snapshot(
        type("Snapshot", (), {"thread_id": "thr_1", "cwd": r"D:\work\alpha", "preview": "seed", "status": "idle", "name": None, "path": None})()
    )
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/status",
        )
    )

    assert messages[0].message_type == "status"
    assert "could not be queried from Codex right now" in messages[0].text


@pytest.mark.asyncio
async def test_stop_swallows_stale_turn_race_and_clears_cached_turn() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/interrupt": [{"id": 2, "error": {"message": "no active turn"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/stop",
        )
    )

    assert messages[0].message_type == "command_result"
    assert "No active turn to stop." in messages[0].text
    assert store.get_active_turn("thr_1") is None
    await client.close()


@pytest.mark.asyncio
async def test_stop_clears_pending_requests_for_interrupted_turn() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/interrupt": [{"id": 2, "error": {"message": "no active turn"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        request_handle="native-r",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/stop",
        )
    )

    assert messages[0].message_type == "command_result"
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is None
    await client.close()


@pytest.mark.asyncio
async def test_attach_thread_preserves_cwd_for_follow_up_new_thread() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/read": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_attached",
                            "cwd": r"D:\work\attached",
                            "preview": "Attached thread",
                            "status": "idle",
                        }
                    },
                }
            ],
            "thread/start": [
                {
                    "id": 3,
                    "result": {
                        "thread": {
                            "id": "thr_new",
                            "cwd": r"D:\work\attached",
                            "preview": "New thread",
                            "status": "idle",
                        }
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    attach_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/thread attach thr_attached",
        )
    )
    new_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="/new",
        )
    )

    assert attach_messages[0].message_type == "status"
    assert new_messages[0].message_type == "status"
    assert "Started thread thr_new." in new_messages[0].text


@pytest.mark.asyncio
async def test_model_override_survives_steer_and_applies_to_next_started_turn() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/steer": [{"id": 2, "result": {"turnId": "turn_1"}}],
            "thread/resume": [
                {
                    "id": 3,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "seed",
                            "status": "idle",
                        }
                    },
                }
            ],
            "turn/start": [{"id": 4, "result": {"turn": {"id": "turn_2", "status": "inProgress"}}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    store.set_next_model_override("qq", "conv-1", "gpt-5.4")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    first = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="keep going",
        )
    )
    store.clear_active_turn("thr_1")
    second = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="now start the next turn",
        )
    )

    assert first[0].message_type == "accepted"
    assert second[0].message_type == "accepted"
    turn_start_payloads = [
        payload["params"]
        for payload in process.inputs
        if payload.get("method") == "turn/start"
    ]
    assert turn_start_payloads[-1]["model"] == "gpt-5.4"
    await client.close()

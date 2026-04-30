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


class CapturingSink:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def send_message(self, message: OutboundMessage) -> None:
        self.messages.append(message)


def _build_service(store: ConversationStore, process: ScriptedProcess, sink: CapturingSink):
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *args: process,
    )
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
    client.add_connection_reset_handler(service.handle_connection_reset)
    client.add_connection_ready_handler(service.handle_connection_ready)
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

    assert messages == []
    assert sink.messages[-1].text == "Hello from Codex"
    await client.close()


@pytest.mark.asyncio
async def test_text_without_cwd_returns_friendly_onboarding_status() -> None:
    process = ScriptedProcess({})
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="hello there",
        )
    )

    assert messages[0].message_type == "status"
    assert "Before we start, I need a working folder." in messages[0].text
    assert "/cwd playground" in messages[0].text
    assert "/cwd <path>" in messages[0].text


@pytest.mark.asyncio
async def test_bridge_emits_started_and_completed_events_for_inbound_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ScriptedProcess({})
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.bridge.core.emit_event", capture_event)

    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m1",
        text="hello there",
    )

    await service.handle_inbound(inbound)

    assert inbound.trace_id is not None
    assert [event["event"] for event in observed_events] == [
        "bridge.inbound.started",
        "bridge.inbound.completed",
    ]
    assert all(event["trace_id"] == inbound.trace_id for event in observed_events)
    assert observed_events[0]["data"]["message_kind"] == "text"
    assert observed_events[1]["data"]["outbound_count"] == 1
    assert observed_events[1]["data"]["outbound_message_types"] == ["status"]


@pytest.mark.asyncio
async def test_stale_resume_clears_binding_and_returns_status_message() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/start": [{"id": 2, "error": {"message": "no rollout found for thread id thr_stale"}}],
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
    assert "Use /threads to pick another thread or /new to start fresh." in messages[0].text
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

    assert messages == []
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

    assert messages == []
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
async def test_batch_approve_replies_to_all_pending_requests() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    for transport_id, suffix in ((91, "abc"), (92, "def")):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=transport_id,
            connection_epoch=1,
        )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve",
        )
    )

    assert messages[0].message_type == "status"
    assert "Recorded accept for 2 requests." in messages[0].text
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") in {91, 92}
    ]
    assert reply_payloads == [
        {"id": 91, "result": {"decision": "accept"}},
        {"id": 92, "result": {"decision": "accept"}},
    ]
    assert store.list_pending_requests("qq", "conv-1") == []
    await client.close()


@pytest.mark.asyncio
async def test_plain_text_with_pending_approvals_cancels_all_then_continues_with_new_turn() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "Recovered thread",
                            "status": "idle",
                        }
                    },
                }
            ],
            "turn/start": [{"id": 3, "result": {"turn": {"id": "turn_2", "status": "inProgress"}}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    for transport_id, suffix in ((91, "abc"), (92, "def")):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=transport_id,
            connection_epoch=1,
        )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="继续说刚刚那个问题",
        )
    )

    assert messages == []
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") in {91, 92}
    ]
    assert reply_payloads == [
        {"id": 91, "result": {"decision": "cancel"}},
        {"id": 92, "result": {"decision": "cancel"}},
    ]
    assert store.list_pending_requests("qq", "conv-1") == []
    assert store.get_active_turn("thr_1") == ("turn_2", "inProgress")
    methods = [payload.get("method") for payload in process.inputs if payload.get("method")]
    assert methods.count("turn/steer") == 0
    assert methods.count("turn/start") == 1
    await client.close()


@pytest.mark.asyncio
async def test_targeted_approve_keeps_other_pending_approvals_active() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    for transport_id, suffix in ((91, "abc"), (92, "def")):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=transport_id,
            connection_epoch=1,
        )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve native-request-abc",
        )
    )

    assert messages[0].message_type == "status"
    assert "Recorded accept for native-request-abc." in messages[0].text
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") in {91, 92}
    ]
    assert reply_payloads == [
        {"id": 91, "result": {"decision": "accept"}},
    ]
    remaining = [route.request_id for route in store.list_pending_requests("qq", "conv-1")]
    assert remaining == ["native-request-def"]
    await client.close()


@pytest.mark.asyncio
async def test_connection_ready_rehydrates_bound_thread_and_replays_native_approval() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [
                {
                    "id": 91,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "requestId": "native-request-abc",
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "command": "Get-Date",
                        "cwd": r"D:\work\alpha",
                        "availableDecisions": ["accept", "cancel"],
                    },
                },
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "Recovered thread",
                            "status": "idle",
                        }
                    },
                },
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)
    service.backend.prefers_native_recovery = lambda: True  # type: ignore[method-assign]

    await client.initialize()

    pending = store.list_pending_requests("qq", "conv-1", kind="approval")
    assert [route.request_id for route in pending] == ["native-request-abc"]

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve",
        )
    )

    assert messages[0].message_type == "status"
    assert "Recorded accept for native-request-abc." in messages[0].text
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") == 91
    ]
    assert reply_payloads == [{"id": 91, "result": {"decision": "accept"}}]
    await client.close()


@pytest.mark.asyncio
async def test_permission_request_is_projected_and_approve_grants_requested_permissions() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    projected = await service.handle_server_request(
        {
            "id": 91,
            "method": "item/permissions/requestApproval",
            "params": {
                "_request_id": "native-request-perms",
                "_transport_request_id": 91,
                "threadId": "thr_1",
                "turnId": "turn_1",
                "reason": "Need access outside the workspace root",
                "permissions": {
                    "fileSystem": {
                        "read": [r"D:\desktop\codex-upstream"],
                    }
                },
            },
        }
    )

    assert projected[0].message_type == "approval_request"
    assert "Permissions:" in projected[0].text
    assert "fileSystem" in projected[0].text

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/approve",
        )
    )

    assert messages[0].message_type == "status"
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") == 91
    ]
    assert reply_payloads == [
        {
            "id": 91,
            "result": {
                "permissions": {
                    "fileSystem": {
                        "read": [r"D:\desktop\codex-upstream"],
                    }
                }
            },
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_unhandled_server_request_is_rejected_instead_of_silently_stalling() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    projected = await service.handle_server_request(
        {
            "id": 77,
            "method": "item/tool/call",
            "params": {
                "_request_id": "native-request-tool",
                "_transport_request_id": 77,
                "threadId": "thr_1",
                "turnId": "turn_1",
                "tool": "lookup_ticket",
                "arguments": {"id": "ABC-123"},
            },
        }
    )

    assert projected[0].message_type == "status"
    assert "unsupported or unroutable request" in projected[0].text
    error_payloads = [
        payload
        for payload in process.inputs
        if "error" in payload and payload.get("id") == 77
    ]
    assert error_payloads == [
        {
            "id": 77,
            "error": {
                "code": -32601,
                "message": "unsupported or unroutable server request: item/tool/call",
                "data": {
                    "reason": "unsupportedServerRequest",
                    "method": "item/tool/call",
                    "requestId": "native-request-tool",
                },
            },
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_connection_reset_evicts_stale_pending_requests_and_interrupts_turn() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/interrupt": [{"id": 2, "result": {"ok": True}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    await service.handle_connection_reset(1)

    assert store.get_active_turn("thr_1") is None
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is None
    interrupt_payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "turn/interrupt"]
    assert interrupt_payloads == [{"threadId": "thr_1", "turnId": "turn_1"}]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_mode", ["dedicated-ws", "shared-ws"])
async def test_connection_reset_in_websocket_mode_evicts_projection_without_interrupting_turn(connection_mode: str) -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)
    client.last_connection_mode = connection_mode

    await service.handle_connection_reset(1)

    assert store.get_active_turn("thr_1") == ("turn_1", "inProgress")
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is None
    interrupt_payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "turn/interrupt"]
    assert interrupt_payloads == []
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_mode", ["dedicated-ws", "shared-ws"])
async def test_connection_ready_in_websocket_mode_clears_stale_active_turn_after_rehydrate(
    connection_mode: str,
) -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/resume": [
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "Recovered thread",
                            "status": "idle",
                        }
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.note_active_turn("thr_1", "turn_1", "inProgress")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)
    service.backend.prefers_native_recovery = lambda: True  # type: ignore[method-assign]

    await client.initialize()

    assert store.get_active_turn("thr_1") is None
    resume_payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "thread/resume"]
    assert resume_payloads == [
        {
            "threadId": "thr_1",
            "serviceName": "imcodex-test",
            "personality": "friendly",
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_stop_command_cleans_up_stale_active_turn_when_native_turn_is_unknown() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/interrupt": [{"id": 2, "error": {"message": "unknown thread"}}],
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
    assert messages[0].text == "No active turn to stop."
    assert store.get_active_turn("thr_1") is None
    await client.close()


@pytest.mark.asyncio
async def test_transient_request_reply_failure_keeps_route_for_retry() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
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
async def test_native_error_reply_failure_returns_status_message() -> None:
    process = ScriptedProcess({"initialize": [{"id": 1, "result": {"ok": True}}]})
    store = ConversationStore(clock=lambda: 1.0)
    store.upsert_pending_request(
        request_id="native-request-abcdef",
        channel_id="qq",
        conversation_id="conv-1",
        thread_id="thr_1",
        turn_id="turn_1",
        kind="approval",
        request_method="item/commandExecution/requestApproval",
        transport_request_id=99,
        connection_epoch=1,
    )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    async def broken_reply_error(
        request_id: str,
        *,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        del code, message, data
        raise AppServerError(f"broken pipe while replying to {request_id}")

    service.backend.reply_error_to_server_request = broken_reply_error  # type: ignore[method-assign]

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/native error native-request-abcdef -32000 failed",
        )
    )

    assert messages[0].message_type == "status"
    assert "could not be sent to Codex right now" in messages[0].text
    assert store.match_pending_request("qq", "conv-1", "native-request-abcdef") is not None
    await client.close()


@pytest.mark.asyncio
async def test_plain_text_cancels_all_pending_approvals_before_submitting_new_input() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "turn/start": [{"id": 2, "result": {"turn": {"id": "turn_2", "status": "inProgress"}}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    for transport_id, suffix in ((91, "abc"), (92, "def")):
        store.upsert_pending_request(
            request_id=f"native-request-{suffix}",
            channel_id="qq",
            conversation_id="conv-1",
            thread_id="thr_1",
            turn_id="turn_1",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            transport_request_id=transport_id,
            connection_epoch=1,
        )
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="skip that and continue",
        )
    )

    assert messages == []
    reply_payloads = [
        payload
        for payload in process.inputs
        if "result" in payload and payload.get("id") in {91, 92}
    ]
    assert reply_payloads == [
        {"id": 91, "result": {"decision": "cancel"}},
        {"id": 92, "result": {"decision": "cancel"}},
    ]
    turn_starts = [payload["params"] for payload in process.inputs if payload.get("method") == "turn/start"]
    assert turn_starts == [
        {
            "threadId": "thr_1",
            "input": [{"type": "text", "text": "skip that and continue"}],
            "summary": "concise",
        }
    ]
    assert store.list_pending_requests("qq", "conv-1") == []
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
async def test_status_command_hides_oversized_upstream_error_details() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/read": [{"id": 2, "error": {"message": "<html>" + ("x" * 500)}}],
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
    assert "<html>" not in messages[0].text
    assert "unexpected upstream error" in messages[0].text.lower()


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
            "thread/list": [
                {
                    "id": 2,
                    "result": {
                        "threads": [
                            {
                                "id": "thr_attached",
                                "cwd": r"D:\work\attached",
                                "preview": "Attached thread",
                                "status": "idle",
                            }
                        ]
                    },
                }
            ],
            "thread/read": [
                {
                    "id": 3,
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
                    "id": 4,
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

    list_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/threads",
        )
    )
    attach_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="/pick 1",
        )
    )
    new_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m3",
            text="/new",
        )
    )

    assert list_messages[0].message_type == "command_result"
    assert attach_messages[0].message_type == "status"
    assert new_messages[0].message_type == "status"
    assert "Switched to Attached thread." in attach_messages[0].text
    assert r"CWD: D:\work\attached" in attach_messages[0].text
    assert "Started thread thr_new." in new_messages[0].text


@pytest.mark.asyncio
async def test_model_command_writes_native_default_model_config() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/value/write": [
                {
                    "id": 2,
                    "result": {
                        "status": "updated",
                        "version": "v1",
                        "filePath": r"D:\Users\me\.codex\config.toml",
                        "overriddenMetadata": None,
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/model gpt-5.4",
        )
    )

    assert messages[0].message_type == "status"
    payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "config/value/write"]
    assert payloads == [{"keyPath": "model", "value": "gpt-5.4", "mergeStrategy": "replace"}]
    await client.close()


@pytest.mark.asyncio
async def test_models_command_reads_native_model_catalog() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "model/list": [
                {
                    "id": 2,
                    "result": {
                        "data": [
                            {
                                "id": "gpt-5.4",
                                "model": "gpt-5.4",
                                "displayName": "GPT-5.4",
                                "description": "Default model",
                                "hidden": False,
                                "supportedReasoningEfforts": [],
                                "defaultReasoningEffort": "medium",
                                "inputModalities": ["text"],
                                "supportsPersonality": True,
                                "isDefault": True,
                                "upgrade": None,
                                "upgradeInfo": None,
                                "availabilityNux": None,
                            }
                        ],
                        "nextCursor": None,
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/model",
        )
    )

    assert messages[0].message_type == "command_result"
    assert "Current:" in messages[0].text
    assert "GPT-5.4" in messages[0].text
    await client.close()


@pytest.mark.asyncio
async def test_model_default_clears_native_default_model_config() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/value/write": [
                {
                    "id": 2,
                    "result": {
                        "status": "updated",
                        "version": "v2",
                        "filePath": r"D:\Users\me\.codex\config.toml",
                        "overriddenMetadata": None,
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/model default",
        )
    )

    assert messages[0].message_type == "status"
    payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "config/value/write"]
    assert payloads == [{"keyPath": "model", "value": None, "mergeStrategy": "replace"}]
    await client.close()


@pytest.mark.asyncio
async def test_config_write_command_sends_native_json_value_write() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/value/write": [
                {
                    "id": 2,
                    "result": {
                        "status": "updated",
                        "version": "v3",
                        "filePath": r"D:\Users\me\.codex\config.toml",
                        "overriddenMetadata": None,
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text='/config write model_reasoning_effort "high"',
        )
    )

    assert messages[0].message_type == "status"
    payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "config/value/write"]
    assert payloads == [{"keyPath": "model_reasoning_effort", "value": "high", "mergeStrategy": "replace"}]
    await client.close()


@pytest.mark.asyncio
async def test_threads_command_uses_default_source_kinds_and_prefers_bound_and_matching_cwd() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [
                {
                    "id": 2,
                    "result": {
                        "threads": [
                            {
                                "id": "thr_other",
                                "cwd": r"D:\work\beta",
                                "preview": "Other thread",
                                "status": "idle",
                                "source": "cli",
                            },
                            {
                                "id": "thr_match",
                                "cwd": r"D:\work\alpha",
                                "preview": "Matching cwd thread",
                                "status": "idle",
                                "source": "vscode",
                            },
                        ]
                    },
                }
            ],
            "thread/read": [
                {
                    "id": 3,
                    "result": {
                        "thread": {
                            "id": "thr_bound",
                            "cwd": r"D:\work\gamma",
                            "preview": "Bound thread",
                            "status": "idle",
                            "source": "appServer",
                        }
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_bound")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/threads",
        )
    )

    assert messages[0].message_type == "command_result"
    lines = messages[0].text.splitlines()
    assert lines[0] == "Threads (Page 1/1)"
    assert lines[1].startswith("1. Bound thread")
    assert "idle" in lines[1]
    assert lines[2].startswith("2. Matching cwd thread")
    assert lines[3].startswith("3. Other thread")
    thread_list_payloads = [
        payload["params"]
        for payload in process.inputs
        if payload.get("method") == "thread/list"
    ]
    assert thread_list_payloads == [
        {
            "sourceKinds": ["cli", "vscode", "appServer"],
            "sortKey": "updated_at",
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_threads_all_command_omits_default_source_filter() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [{"id": 2, "result": {"threads": []}}],
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
            text="/threads --all",
        )
    )

    assert messages[0].message_type == "command_result"
    thread_list_payloads = [
        payload["params"]
        for payload in process.inputs
        if payload.get("method") == "thread/list"
    ]
    assert thread_list_payloads == [{"sortKey": "updated_at"}]
    await client.close()


@pytest.mark.asyncio
async def test_status_and_thread_read_render_transport_mode_and_thread_source() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/read": [
                {
                    "id": 2,
                    "result": {
                        "config": {
                            "model": "gpt-5.4",
                            "approval_policy": "on-request",
                            "sandbox_mode": "workspace-write",
                        }
                    },
                }
            ],
            "thread/read": [
                {
                    "id": 3,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "seed",
                            "status": "idle",
                            "path": r"D:\work\alpha",
                            "source": "appServer",
                        }
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "thread": {
                            "id": "thr_1",
                            "cwd": r"D:\work\alpha",
                            "preview": "seed",
                            "status": "idle",
                            "path": r"D:\work\alpha",
                            "source": "appServer",
                        }
                    },
                },
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    status_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/status",
        )
    )
    thread_messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m2",
            text="/thread read",
        )
    )

    assert "Status" in status_messages[0].text
    assert "CWD: D:\\work\\alpha" in status_messages[0].text
    assert "Model: gpt-5.4" in status_messages[0].text
    assert "Permissions: Default" in status_messages[0].text
    assert "Bridge visibility: Standard" in status_messages[0].text
    assert "Source: appServer" in thread_messages[0].text
    await client.close()


@pytest.mark.asyncio
async def test_threads_command_supports_query_filter_and_page_window() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "thread/list": [
                {
                    "id": 2,
                    "result": {
                        "threads": [
                            {"id": "thr_1", "cwd": r"D:\work\a", "preview": "Alpha project", "status": "idle", "source": "cli"},
                            {"id": "thr_2", "cwd": r"D:\work\b", "preview": "Alpha notes", "status": "idle", "source": "cli"},
                            {"id": "thr_3", "cwd": r"D:\work\c", "preview": "Alpha tests", "status": "idle", "source": "cli"},
                            {"id": "thr_4", "cwd": r"D:\work\d", "preview": "Alpha docs", "status": "idle", "source": "cli"},
                            {"id": "thr_5", "cwd": r"D:\work\e", "preview": "Alpha deploy", "status": "idle", "source": "cli"},
                            {"id": "thr_6", "cwd": r"D:\work\f", "preview": "Alpha release", "status": "idle", "source": "cli"},
                        ]
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\a")
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/threads alpha --page 2",
        )
    )

    lines = messages[0].text.splitlines()
    assert lines[0] == "Threads (Page 2/2)"
    assert lines[1].startswith("1. Alpha release")
    assert "/prev" in lines[-1]
    await client.close()


@pytest.mark.asyncio
async def test_next_without_thread_browser_context_prompts_user_to_open_threads_first() -> None:
    process = ScriptedProcess({})
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    _client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/next",
        )
    )

    assert messages[0].message_type == "error"
    assert "Use /threads first." in messages[0].text


@pytest.mark.asyncio
async def test_permission_command_writes_native_permission_preset() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/batchWrite": [{"id": 2, "result": {"status": "updated"}}],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/permission full-access",
        )
    )

    assert messages[0].message_type == "status"
    payloads = [payload["params"] for payload in process.inputs if payload.get("method") == "config/batchWrite"]
    assert payloads == [
        {
            "edits": [
                {"keyPath": "approval_policy", "value": "never", "mergeStrategy": "replace"},
                {"keyPath": "sandbox_mode", "value": "danger-full-access", "mergeStrategy": "replace"},
            ],
            "reloadUserConfig": False,
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_permission_without_arg_shows_permission_browser() -> None:
    process = ScriptedProcess(
        {
            "initialize": [{"id": 1, "result": {"ok": True}}],
            "config/read": [
                {
                    "id": 2,
                    "result": {
                        "config": {
                            "approval_policy": "on-request",
                            "sandbox_mode": "workspace-write",
                        }
                    },
                }
            ],
        }
    )
    store = ConversationStore(clock=lambda: 1.0)
    sink = CapturingSink()
    client, service = _build_service(store, process, sink)

    messages = await service.handle_inbound(
        InboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="u1",
            message_id="m1",
            text="/permission",
        )
    )

    assert messages[0].message_type == "command_result"
    assert "Permission Modes" in messages[0].text
    assert "Current: Default" in messages[0].text
    assert "/permission read-only" in messages[0].text
    await client.close()

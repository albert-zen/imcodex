from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any

from imcodex.appserver import AppServerClient, AppServerSupervisor, CodexBackend
from imcodex.appserver.thread_dynamic_tools import (
    native_thread_dynamic_tool_specs,
)
from imcodex.bridge import BridgeService, CommandRouter, MessageProjector
from imcodex.channels import MultiplexOutboundSink
from imcodex.channels.middleware import UnifiedChannelMiddleware
from imcodex.store import ConversationStore


@dataclass(frozen=True, slots=True)
class NativeStep:
    """One deterministic response from the fake native App Server boundary."""

    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    notifications: tuple[dict[str, Any], ...] = ()
    expected_params: dict[str, Any] | None = None
    params_validator: Callable[[dict[str, Any]], bool] | None = None
    expects_no_params: bool = False


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class _FakeStdin:
    def __init__(self, process: ScriptedNativeProcess) -> None:
        self.process = process
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            self.process.on_input(line.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.closed = True


class ScriptedNativeProcess:
    """A JSONL App Server test double with ordered, per-method responses."""

    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stderr = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self.inputs: list[dict[str, Any]] = []
        self.closed = False
        self.returncode: int | None = None
        self._steps: dict[str, deque[NativeStep]] = defaultdict(deque)
        self.unexpected_requests: list[dict[str, Any]] = []
        self.parameter_mismatches: list[dict[str, Any]] = []

    def queue(self, method: str, *steps: NativeStep) -> None:
        self._steps[method].extend(steps)

    def on_input(self, raw: str) -> None:
        request = json.loads(raw)
        self.inputs.append(request)
        method = request.get("method")
        if method in {None, "initialized"}:
            return
        steps = self._steps.get(str(method))
        if not steps:
            self.unexpected_requests.append(request)
            if "id" in request:
                self._put(
                    {
                        "id": request["id"],
                        "error": {
                            "code": -32601,
                            "message": f"No scripted response for {method}",
                        },
                    }
                )
            return
        step = steps.popleft()
        if (
            step.expected_params is not None
            and request.get("params") != step.expected_params
        ):
            self.parameter_mismatches.append(
                {
                    "method": method,
                    "expected": step.expected_params,
                    "actual": request.get("params"),
                }
            )
        if step.params_validator is not None:
            params = request.get("params")
            if not isinstance(params, dict) or not step.params_validator(params):
                self.parameter_mismatches.append(
                    {
                        "method": method,
                        "expected": "custom parameter contract",
                        "actual": params,
                    }
                )
        if step.expects_no_params and "params" in request:
            self.parameter_mismatches.append(
                {
                    "method": method,
                    "expected": "no params member",
                    "actual": request.get("params"),
                }
            )
        response: dict[str, Any] = {"id": request["id"]}
        if step.error is not None:
            response["error"] = step.error
        else:
            response["result"] = step.result or {}
        self._put(response)
        for notification in step.notifications:
            self._put(notification)

    def requests(self, method: str) -> list[dict[str, Any]]:
        return [item for item in self.inputs if item.get("method") == method]

    def assert_consumed(self, *, excluding: Iterable[str] = ()) -> None:
        excluded = set(excluding)
        remaining = {
            method: len(steps)
            for method, steps in self._steps.items()
            if steps and method not in excluded
        }
        assert self.unexpected_requests == []
        assert self.parameter_mismatches == []
        assert remaining == {}

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.lines.put_nowait(b"")

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def _put(self, payload: dict[str, Any]) -> None:
        self.stdout.lines.put_nowait(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )


@dataclass(slots=True)
class SystemHarness:
    process: ScriptedNativeProcess
    store: ConversationStore
    client: AppServerClient
    service: BridgeService
    middleware: UnifiedChannelMiddleware
    outbound: MultiplexOutboundSink
    closeables: list[Any] = field(default_factory=list)

    async def close(self) -> None:
        try:
            await self.service.close()
        finally:
            try:
                await self.client.close()
            finally:
                try:
                    await self.store.flush_pending_writes()
                finally:
                    for closeable in reversed(self.closeables):
                        close = getattr(closeable, "aclose", None)
                        if close is None:
                            close = getattr(closeable, "close", None)
                        if close is None:
                            continue
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result


def build_system_harness(
    tmp_path: Path,
    process: ScriptedNativeProcess,
) -> SystemHarness:
    store = ConversationStore(
        state_path=tmp_path / "conversation-state.json",
        clock=time.time,
    )
    supervisor = AppServerSupervisor(
        codex_bin="codex",
        core_mode="spawned-stdio",
        spawn_process=lambda *_args: process,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={
            "name": "imcodex-system-test",
            "title": "IMCodex system test",
            "version": "0.1.0",
        },
        experimental_api_enabled=True,
    )
    outbound = MultiplexOutboundSink()
    service = BridgeService(
        store=store,
        backend=CodexBackend(
            client=client,
            store=store,
            service_name="imcodex-system-test",
            thread_dynamic_tools=native_thread_dynamic_tool_specs(),
        ),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=outbound,
    )
    middleware = UnifiedChannelMiddleware(service=service)
    client.add_notification_handler(service.handle_notification)
    client.add_server_request_handler(service.handle_server_request)
    client.add_connection_reset_handler(service.handle_connection_reset)
    client.add_connection_ready_handler(service.handle_connection_ready)
    return SystemHarness(
        process=process,
        store=store,
        client=client,
        service=service,
        middleware=middleware,
        outbound=outbound,
    )


def queue_new_thread_turn(
    process: ScriptedNativeProcess,
    *,
    thread_id: str,
    turn_id: str,
    cwd: str,
    answer: str,
    expected_input: list[dict[str, Any]] | None = None,
    input_validator: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> None:
    if (expected_input is None) == (input_validator is None):
        raise ValueError("Provide exactly one of expected_input or input_validator")

    def validate_turn_start(params: dict[str, Any]) -> bool:
        if set(params) != {"threadId", "input", "summary"}:
            return False
        if params["threadId"] != thread_id or params["summary"] != "concise":
            return False
        native_input = params["input"]
        if not isinstance(native_input, list):
            return False
        if expected_input is not None:
            return native_input == expected_input
        assert input_validator is not None
        return input_validator(native_input)

    process.queue(
        "thread/start",
        NativeStep(
            result={
                "thread": {
                    "id": thread_id,
                    "cwd": cwd,
                    "preview": "System test thread",
                    "status": "idle",
                }
            },
            expected_params={
                "cwd": cwd,
                "serviceName": "imcodex-system-test",
                "dynamicTools": native_thread_dynamic_tool_specs(),
            },
        ),
    )
    process.queue(
        "turn/start",
        NativeStep(
            result={"turn": {"id": turn_id, "status": "inProgress"}},
            notifications=(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": f"{turn_id}-answer",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": answer,
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": "completed"},
                    },
                },
            ),
            params_validator=validate_turn_start,
        ),
    )


async def wait_until(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("system-test condition was not satisfied")
        await asyncio.sleep(min(0.01, remaining))

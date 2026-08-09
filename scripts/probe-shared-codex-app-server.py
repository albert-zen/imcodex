#!/usr/bin/env python3
"""Storage- and protocol-isolated two-client probe for a native App Server.

The probe copies an existing auth file without reading or logging its contents,
starts an isolated App Server on a temporary Unix socket, and connects two
clients through a stateless JSONL-to-Unix-WebSocket shim using T3's fixed
``<binary> app-server`` process shape. Output is limited to protocol methods,
native identifiers, and pass/fail assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve


JSON = dict[str, Any]


class ProbeFailure(RuntimeError):
    pass


def _stage(name: str) -> None:
    print(json.dumps({"stage": name}, separators=(",", ":")), flush=True)


class JsonlClient:
    def __init__(self, name: str, process: asyncio.subprocess.Process) -> None:
        self.name = name
        self.process = process
        self.messages: list[JSON] = []
        self.pending: dict[int | str, asyncio.Future[JSON]] = {}
        self.server_requests: dict[int | str, JSON] = {}
        self._next_id = 1
        self._changed = asyncio.Condition()
        self._reader_task = asyncio.create_task(self._read(), name=f"probe-reader-{name}")
        assert process.stderr is not None
        self._stderr_task = asyncio.create_task(process.stderr.read(), name=f"probe-stderr-{name}")

    @classmethod
    async def connect(
        cls,
        name: str,
        *,
        shim_path: Path,
        shim_bin: Path | None,
        socket_path: Path,
        env: dict[str, str],
        t3_chat_sync_shape: bool = False,
    ) -> "JsonlClient":
        shim_env = dict(env)
        if shim_bin is None:
            shim_env["IMCODEX_SHARED_CODEX_SOCKET"] = str(socket_path)
            command = [sys.executable, str(shim_path), "app-server"]
        else:
            command = [
                str(shim_bin),
                "app-server",
                "--connect",
                f"unix://{socket_path}",
            ]
            if t3_chat_sync_shape:
                command.extend(
                    [
                        "--chat-sync-without-t3-mcp",
                        "-c",
                        "mcp_servers.t3-code.url=http://127.0.0.1:9/mcp",
                        "-c",
                        'mcp_servers.t3-code.bearer_token_env_var="T3_MCP_BEARER_TOKEN"',
                    ]
                )
                shim_env["T3_MCP_BEARER_TOKEN"] = "unused-isolated-probe-value"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=shim_env,
        )
        return cls(name, process)

    async def _read(self) -> None:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeFailure(f"{self.name} emitted non-JSON stdout") from exc
            if not isinstance(message, dict):
                raise ProbeFailure(f"{self.name} emitted a non-object message")
            request_id = message.get("id")
            if request_id in self.pending and ("result" in message or "error" in message):
                future = self.pending.pop(request_id)
                if not future.done():
                    future.set_result(message)
            else:
                self.messages.append(message)
                if request_id is not None and isinstance(message.get("method"), str):
                    self.server_requests[request_id] = message
                if message.get("method") == "serverRequest/resolved":
                    resolved_id = (message.get("params") or {}).get("requestId")
                    self.server_requests.pop(resolved_id, None)
                async with self._changed:
                    self._changed.notify_all()

    async def send(self, message: JSON) -> None:
        if self.process.stdin is None:
            raise ProbeFailure(f"{self.name} stdin is closed")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: JSON | None = None, timeout: float = 30) -> JSON:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JSON] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message: JSON = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)
        try:
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ProbeFailure(
                f"{self.name} {method} response timed out ({self._diagnostic_state()})"
            ) from exc
        if "error" in response:
            raise ProbeFailure(f"{self.name} {method} failed: {_safe_error(response['error'])}")
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def request_result(
        self, method: str, params: JSON | None = None, timeout: float = 30
    ) -> JSON:
        """Return the full JSON-RPC response, including an expected error."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JSON] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message: JSON = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ProbeFailure(
                f"{self.name} {method} response timed out ({self._diagnostic_state()})"
            ) from exc

    def _diagnostic_state(self) -> str:
        reader = "running"
        if self._reader_task.done():
            if self._reader_task.cancelled():
                reader = "cancelled"
            else:
                error = self._reader_task.exception()
                reader = "finished" if error is None else f"failed:{type(error).__name__}"
        stderr = "pending"
        if self._stderr_task.done() and not self._stderr_task.cancelled():
            stderr = f"{len(self._stderr_task.result())}bytes"
        return (
            f"proxyReturnCode={self.process.returncode},reader={reader},messages={len(self.messages)},"
            f"stderr={stderr}"
        )

    async def notify(self, method: str, params: JSON | None = None) -> None:
        message: JSON = {"method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)

    async def respond(self, request_id: int | str, result: JSON) -> None:
        await self.send({"id": request_id, "result": result})

    async def wait_for(
        self,
        predicate: Callable[[JSON], bool],
        *,
        start: int = 0,
        timeout: float = 90,
    ) -> tuple[int, JSON]:
        deadline = time.monotonic() + timeout
        while True:
            for index in range(start, len(self.messages)):
                message = self.messages[index]
                if predicate(message):
                    return index, message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(
                    f"{self.name} timed out; observed={_safe_event_signatures(self.messages[start:])}"
                )
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    raise ProbeFailure(
                        f"{self.name} timed out; "
                        f"observed={_safe_event_signatures(self.messages[start:])}"
                    ) from exc

    async def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            self.process.terminate()
            await self.process.wait()
        self._reader_task.cancel()
        self._stderr_task.cancel()
        await asyncio.gather(self._reader_task, self._stderr_task, return_exceptions=True)


def _safe_error(error: Any) -> str:
    if not isinstance(error, dict):
        return type(error).__name__
    summary: JSON = {"type": type(error).__name__}
    if isinstance(error.get("code"), int) and not isinstance(error["code"], bool):
        summary["code"] = error["code"]
    return json.dumps(summary, separators=(",", ":"))


_PROTOCOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,79}$")


def _protocol_name(value: Any) -> str | None:
    return value if isinstance(value, str) and _PROTOCOL_NAME.fullmatch(value) else None


def _numeric_code(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_event_signatures(messages: list[JSON]) -> str:
    signatures: list[JSON] = []
    for message in messages:
        signature: JSON = {"method": _protocol_name(message.get("method")) or "response"}
        params = _params(message)
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        codex_error = (
            error.get("codexErrorInfo")
            if isinstance(error.get("codexErrorInfo"), dict)
            else {}
        )
        type_name = _protocol_name(_item_type(message)) or _protocol_name(
            codex_error.get("type")
        )
        if type_name is not None:
            signature["type"] = type_name
        response_error = message.get("error") if isinstance(message.get("error"), dict) else {}
        code = _numeric_code(response_error.get("code"))
        if code is None:
            code = _numeric_code(error.get("code"))
        if code is not None:
            signature["code"] = code
        signatures.append(signature)
    return json.dumps(signatures, separators=(",", ":"))


def _params(message: JSON) -> JSON:
    value = message.get("params")
    return value if isinstance(value, dict) else {}


def _thread_id(message: JSON) -> str | None:
    params = _params(message)
    thread = params.get("thread")
    return str(params.get("threadId") or (thread.get("id") if isinstance(thread, dict) else "")) or None


def _turn_id(message: JSON) -> str | None:
    params = _params(message)
    turn = params.get("turn")
    return str(params.get("turnId") or (turn.get("id") if isinstance(turn, dict) else "")) or None


def _item(message: JSON) -> JSON:
    value = _params(message).get("item")
    return value if isinstance(value, dict) else {}


def _item_type(message: JSON) -> str | None:
    value = _item(message).get("type")
    return str(value) if value is not None else None


def _item_id(message: JSON) -> str | None:
    value = _item(message).get("id") or _params(message).get("itemId")
    return str(value) if value is not None else None


def _item_text(message: JSON) -> str:
    item = _item(message)
    parts: list[str] = []
    for key in ("text", "command", "aggregatedOutput"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(part) for part in value)
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


def _turn_status(message: JSON) -> str | None:
    turn = _params(message).get("turn")
    if not isinstance(turn, dict) or turn.get("status") is None:
        return None
    return str(turn["status"])


async def _initialize(client: JsonlClient) -> None:
    await client.request(
        "initialize",
        {
            "clientInfo": {"name": f"imcodex-{client.name}-probe", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
        },
    )
    await client.notify("initialized")


async def _stdio_shim() -> int:
    """Translate one JSONL stdio client into one Unix-WebSocket connection."""
    socket_value = os.environ.get("IMCODEX_SHARED_CODEX_SOCKET", "")
    if not socket_value:
        return 2

    async with unix_connect(
        socket_value,
        uri="ws://localhost/",
        compression=None,
        max_size=None,
        open_timeout=5,
    ) as websocket:
        async def stdin_to_websocket() -> None:
            while line := await asyncio.to_thread(sys.stdin.buffer.readline):
                await websocket.send(line.decode("utf-8").rstrip("\r\n"))

        async def websocket_to_stdout() -> None:
            async for message in websocket:
                if not isinstance(message, str):
                    raise ProbeFailure("shim received a binary WebSocket frame")
                sys.stdout.write(message + "\n")
                sys.stdout.flush()

        upstream = asyncio.create_task(stdin_to_websocket())
        downstream = asyncio.create_task(websocket_to_stdout())
        done, pending = await asyncio.wait(
            (upstream, downstream), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    return 0


def _text_input(text: str) -> list[JSON]:
    return [{"type": "text", "text": text, "text_elements": []}]


def _thread_from(result: JSON) -> JSON:
    thread = result.get("thread")
    if not isinstance(thread, dict) or not thread.get("id"):
        raise ProbeFailure("native response omitted thread.id")
    return thread


def _turn_from(result: JSON) -> JSON:
    turn = result.get("turn")
    if not isinstance(turn, dict) or not turn.get("id"):
        raise ProbeFailure("native response omitted turn.id")
    return turn


async def _start_thread(
    client: JsonlClient,
    cwd: Path,
    *,
    approvals: str = "on-request",
    sandbox: str = "workspace-write",
) -> JSON:
    return _thread_from(
        await client.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": approvals,
                "sandbox": sandbox,
                "experimentalRawEvents": False,
            },
        )
    )


async def _resume(client: JsonlClient, thread_id: str) -> JSON:
    return _thread_from(await client.request("thread/resume", {"threadId": thread_id}))


def _event_identity(message: JSON) -> tuple[str | None, str | None, str | None]:
    return _thread_id(message), _turn_id(message), _item_id(message)


def _assert_turn_projection(
    label: str,
    initiator: JsonlClient,
    observer: JsonlClient,
    initiator_start: int,
    observer_start: int,
    thread_id: str,
    turn_id: str,
) -> dict[str, Any]:
    categories = {
        "user": lambda message: _item_type(message) == "userMessage",
        "agent": lambda message: (
            _item_type(message) == "agentMessage" and label in _item_text(message)
        ),
        "tool": lambda message: (
            _item_type(message) == "commandExecution"
            and _item(message).get("status") == "completed"
            and "printf" in _item_text(message)
        ),
        "completed": lambda message: (
            message.get("method") == "turn/completed" and _turn_status(message) == "completed"
        ),
    }
    summary: dict[str, Any] = {"label": label, "threadId": thread_id, "turnId": turn_id}
    for category, matches in categories.items():
        left = [
            message
            for message in initiator.messages[initiator_start:]
            if _thread_id(message) == thread_id
            and _turn_id(message) == turn_id
            and matches(message)
        ]
        right = [
            message
            for message in observer.messages[observer_start:]
            if _thread_id(message) == thread_id
            and _turn_id(message) == turn_id
            and matches(message)
        ]
        if not left or not right:
            raise ProbeFailure(f"{label}: missing {category} event on one client")
        left_ids = {_event_identity(message) for message in left}
        right_ids = {_event_identity(message) for message in right}
        common = left_ids & right_ids
        if not common:
            raise ProbeFailure(f"{label}: {category} native identities differ")
        summary[category] = sorted(common)[0]

    for client, start in ((initiator, initiator_start), (observer, observer_start)):
        user_messages = [
            message
            for message in client.messages[start:]
            if _thread_id(message) == thread_id
            and _turn_id(message) == turn_id
            and _item_type(message) == "userMessage"
        ]
        user_ids = {_item_id(message) for message in user_messages}
        completed_user_messages = [
            message for message in user_messages if message.get("method") == "item/completed"
        ]
        if len(user_ids) != 1 or len(completed_user_messages) != 1:
            raise ProbeFailure(
                f"{label}: {client.name} saw duplicate/echoed user items: "
                f"uniqueIds={len(user_ids)},completedNotifications={len(completed_user_messages)}"
            )
    summary["noEcho"] = True
    return summary


async def _run_tool_turn(
    initiator: JsonlClient,
    observer: JsonlClient,
    thread_id: str,
    marker: str,
) -> dict[str, Any]:
    initiator_start = len(initiator.messages)
    observer_start = len(observer.messages)
    result = await initiator.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": _text_input(
                "Use the shell tool exactly once to run `printf 'shared-probe-tool'`. "
                f"Then reply with exactly {marker}."
            ),
            "clientUserMessageId": f"probe-{marker.lower()}",
        },
    )
    turn_id = str(_turn_from(result)["id"])
    predicate = lambda message: (
        message.get("method") == "turn/completed"
        and _thread_id(message) == thread_id
        and _turn_id(message) == turn_id
        and _turn_status(message) == "completed"
    )
    await initiator.wait_for(predicate, start=initiator_start, timeout=120)
    await observer.wait_for(predicate, start=observer_start, timeout=120)
    return _assert_turn_projection(
        marker,
        initiator,
        observer,
        initiator_start,
        observer_start,
        thread_id,
        turn_id,
    )


async def _bootstrap_persisted_thread(client: JsonlClient, thread_id: str) -> str:
    start = len(client.messages)
    result = await client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": _text_input("Reply exactly BOOTSTRAP_READY."),
            "clientUserMessageId": "probe-bootstrap",
        },
    )
    turn_id = str(_turn_from(result)["id"])
    await client.wait_for(
        lambda message: (
            message.get("method") == "turn/completed"
            and _thread_id(message) == thread_id
            and _turn_id(message) == turn_id
            and _turn_status(message) == "completed"
        ),
        start=start,
        timeout=120,
    )
    await client.wait_for(
        lambda message: (
            _item_type(message) == "agentMessage"
            and _thread_id(message) == thread_id
            and _turn_id(message) == turn_id
            and "BOOTSTRAP_READY" in _item_text(message)
        ),
        start=start,
        timeout=30,
    )
    return turn_id


async def _approval_probe(
    a: JsonlClient,
    b: JsonlClient,
    *,
    cwd: Path,
    reconnect: Callable[[str], Any],
    deterministic: bool = False,
) -> tuple[dict[str, Any], JsonlClient]:
    marker_path = cwd / "approval-probe-must-not-exist"
    marker_path.unlink(missing_ok=True)
    thread = await _start_thread(
        a,
        cwd,
        approvals="on-request" if deterministic else "untrusted",
        sandbox="read-only" if deterministic else "workspace-write",
    )
    thread_id = str(thread["id"])
    await _bootstrap_persisted_thread(a, thread_id)
    await _resume(b, thread_id)
    start_a = len(a.messages)
    start_b = len(b.messages)
    result = await a.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": _text_input(
                (
                    "Immediately use the shell tool exactly once to run "
                    "`touch approval-probe-must-not-exist` in the current workspace. "
                    "Do not use another tool before it. If it is declined, reply APPROVAL_DONE."
                    if deterministic
                    else "Use the shell tool exactly once to run `printf 'approval-probe'`, "
                    "then reply APPROVAL_DONE."
                )
            ),
            "clientUserMessageId": "probe-approval",
        },
    )
    turn_id = str(_turn_from(result)["id"])
    is_approval = lambda message: str(message.get("method") or "").endswith("/requestApproval")
    _, req_a = await a.wait_for(is_approval, start=start_a, timeout=120)
    _, req_b = await b.wait_for(is_approval, start=start_b, timeout=120)
    if req_a.get("id") != req_b.get("id"):
        raise ProbeFailure("approval request IDs differ across clients")
    request_id = req_a["id"]
    method = str(req_a["method"])
    await b.close()
    b2: JsonlClient = await reconnect("client-b-reconnected")
    await _initialize(b2)
    resume_start = len(b2.messages)
    resumed = await _resume(b2, thread_id)
    if str(resumed["id"]) != thread_id:
        raise ProbeFailure("reconnected approval client resumed a different thread")
    _, replay = await b2.wait_for(is_approval, start=resume_start, timeout=30)
    if replay.get("id") != request_id:
        raise ProbeFailure("pending approval replay used a different request ID")
    await a.respond(request_id, {"decision": "decline"})
    resolved = lambda message: (
        message.get("method") == "serverRequest/resolved"
        and _params(message).get("requestId") == request_id
    )
    await a.wait_for(resolved, start=start_a, timeout=30)
    await b2.wait_for(resolved, start=resume_start, timeout=30)
    if request_id in a.server_requests or request_id in b2.server_requests:
        raise ProbeFailure("resolved approval remained actionable")
    completed = lambda message: (
        message.get("method") == "turn/completed"
        and _thread_id(message) == thread_id
        and _turn_id(message) == turn_id
    )
    _, completed_a = await a.wait_for(completed, start=start_a, timeout=120)
    _, completed_b = await b2.wait_for(completed, start=resume_start, timeout=120)
    if _turn_status(completed_a) != "completed" or _turn_status(completed_b) != "completed":
        raise ProbeFailure("approval Turn did not complete successfully after decline")
    if marker_path.exists():
        raise ProbeFailure("declined approval unexpectedly produced the marker file")
    return (
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "requestId": request_id,
            "method": method,
            "replayed": True,
            "resolvedOnBoth": True,
            "staleActionable": False,
            "writePrevented": True,
        },
        b2,
    )


async def _active_reconnect_probe(
    a: JsonlClient,
    b: JsonlClient,
    *,
    thread_id: str,
    reconnect: Callable[[str], Any],
) -> tuple[dict[str, Any], JsonlClient]:
    start_a = len(a.messages)
    result = await a.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": _text_input(
                "Use the shell tool exactly once to run `sleep 4`, then reply exactly RECONNECT_DONE."
            ),
            "clientUserMessageId": "probe-reconnect",
        },
    )
    turn_id = str(_turn_from(result)["id"])
    started = lambda message: (
        message.get("method") == "turn/started"
        and _thread_id(message) == thread_id
        and _turn_id(message) == turn_id
    )
    await b.wait_for(started, timeout=30)
    await b.close()
    b2: JsonlClient = await reconnect("client-b-active-reconnected")
    await _initialize(b2)
    resume_start = len(b2.messages)
    resumed = await _resume(b2, thread_id)
    if str(resumed["id"]) != thread_id:
        raise ProbeFailure("active reconnect resumed a different thread")
    completed = lambda message: (
        message.get("method") == "turn/completed"
        and _thread_id(message) == thread_id
        and _turn_id(message) == turn_id
    )
    _, completed_a = await a.wait_for(completed, start=start_a, timeout=120)
    _, completed_b = await b2.wait_for(completed, start=resume_start, timeout=120)
    if _turn_status(completed_a) != "completed" or _turn_status(completed_b) != "completed":
        raise ProbeFailure("active reconnect Turn did not complete successfully")
    snapshot = await b2.request("thread/read", {"threadId": thread_id, "includeTurns": True})
    turns = (_thread_from(snapshot).get("turns") or [])
    matches = [turn for turn in turns if isinstance(turn, dict) and str(turn.get("id")) == turn_id]
    if len(matches) != 1 or matches[0].get("status") != "completed":
        raise ProbeFailure("reconnected snapshot did not contain one completed turn")
    completed_count = sum(
        1
        for message in b2.messages[resume_start:]
        if completed(message)
    )
    if completed_count != 1:
        raise ProbeFailure(f"reconnected client saw {completed_count} completion events")
    return (
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "terminalSnapshot": True,
            "completionCount": completed_count,
        },
        b2,
    )


async def _conflict_probe(a: JsonlClient, b: JsonlClient, thread_id: str) -> dict[str, Any]:
    params_a = {
        "threadId": thread_id,
        "input": _text_input("Use the shell tool to run `sleep 4`, then reply CONFLICT_A."),
        "clientUserMessageId": "probe-conflict-a",
    }
    params_b = {
        "threadId": thread_id,
        "input": _text_input("Reply exactly CONFLICT_B."),
        "clientUserMessageId": "probe-conflict-b",
    }
    response_a, response_b = await asyncio.gather(
        a.request_result("turn/start", params_a, timeout=30),
        b.request_result("turn/start", params_b, timeout=30),
    )
    successes = [response for response in (response_a, response_b) if "result" in response]
    failures = [response for response in (response_a, response_b) if "error" in response]
    if len(successes) != 1 or len(failures) != 1:
        turn_ids = [
            str(_turn_from(response["result"])["id"])
            for response in successes
        ]
        lifecycle: dict[str, list[tuple[str, str, str | None]]] = {a.name: [], b.name: []}
        seen: set[tuple[str, str, str]] = set()
        deadline = time.monotonic() + 120
        while not all(
            any((client.name, "turn/completed", turn_id) in seen for client in (a, b))
            for turn_id in turn_ids
        ):
            for client in (a, b):
                for message in client.messages:
                    method = str(message.get("method") or "")
                    turn_id = _turn_id(message)
                    key = (client.name, method, str(turn_id))
                    if (
                        method not in {"turn/started", "turn/completed"}
                        or turn_id not in turn_ids
                        or key in seen
                    ):
                        continue
                    seen.add(key)
                    lifecycle[client.name].append((method, str(turn_id), _turn_status(message)))
            if all(
                any((client.name, "turn/completed", turn_id) in seen for client in (a, b))
                for turn_id in turn_ids
            ):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        snapshot = await a.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        turns = _thread_from(snapshot).get("turns") or []
        statuses = {
            str(turn.get("id")): str(turn.get("status"))
            for turn in turns
            if isinstance(turn, dict) and str(turn.get("id")) in turn_ids
        }
        raise ProbeFailure(
            "concurrent turn/start was not explicit one-winner conflict: "
            f"successes={len(successes)} failures={len(failures)} "
            f"turnIds={turn_ids} lifecycle={lifecycle} statuses={statuses}"
        )
    winning_turn = _turn_from(successes[0]["result"])
    turn_id = str(winning_turn["id"])
    completed = lambda message: (
        message.get("method") == "turn/completed"
        and _thread_id(message) == thread_id
        and _turn_id(message) == turn_id
    )
    await a.wait_for(completed, timeout=120)
    await b.wait_for(completed, timeout=120)
    resumed_a = await _resume(a, thread_id)
    resumed_b = await _resume(b, thread_id)
    if str(resumed_a["id"]) != thread_id or str(resumed_b["id"]) != thread_id:
        raise ProbeFailure("clients could not recover by resuming after conflict")
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "error": _safe_error(failures[0]["error"]),
        "oneWinner": True,
        "recoverable": True,
    }


@contextlib.asynccontextmanager
async def _isolated_app_server(
    codex_bin: Path,
    *,
    socket_path: Path,
    workspace: Path,
    env: dict[str, str],
):
    server: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    try:
        server = await asyncio.create_subprocess_exec(
            str(codex_bin),
            "app-server",
            "--listen",
            f"unix://{socket_path}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workspace,
            start_new_session=True,
        )
        assert server.stderr is not None
        stderr_task = asyncio.create_task(server.stderr.read(), name="probe-app-server-stderr")
        deadline = time.monotonic() + 15
        while not socket_path.exists():
            if server.returncode is not None:
                stderr_size = len(await stderr_task)
                raise ProbeFailure(
                    f"isolated App Server exited before readiness: "
                    f"returnCode={server.returncode},stderr={stderr_size}bytes"
                )
            if time.monotonic() >= deadline:
                raise ProbeFailure("isolated App Server socket did not appear")
            await asyncio.sleep(0.05)
        yield
    finally:
        if server is not None:
            await _terminate_isolated_process_tree(server)
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, 5)
            except asyncio.TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)


@contextlib.asynccontextmanager
async def _reload_guard(
    native_socket_path: Path,
    guard_socket_path: Path,
    forwarded_reload: list[bool],
):
    async def handler(client_websocket) -> None:
        async with unix_connect(
            str(native_socket_path),
            uri="ws://localhost/",
            compression=None,
            max_size=None,
            ping_interval=None,
        ) as native_websocket:

            async def client_to_native() -> None:
                async for message in client_websocket:
                    if isinstance(message, str):
                        try:
                            decoded = json.loads(message)
                        except json.JSONDecodeError:
                            decoded = None
                        if (
                            isinstance(decoded, dict)
                            and decoded.get("method") == "config/mcpServer/reload"
                        ):
                            forwarded_reload.append(True)
                            request_id = decoded.get("id")
                            await client_websocket.send(
                                json.dumps(
                                    {
                                        "id": request_id,
                                        "error": {"code": -32600, "message": "blocked by probe"},
                                    },
                                    separators=(",", ":"),
                                )
                            )
                            continue
                    await native_websocket.send(message)

            async def native_to_client() -> None:
                async for message in native_websocket:
                    await client_websocket.send(message)

            upstream = asyncio.create_task(client_to_native(), name="probe-guard-upstream")
            downstream = asyncio.create_task(native_to_client(), name="probe-guard-downstream")
            done, pending = await asyncio.wait(
                (upstream, downstream),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    async with unix_serve(
        handler,
        str(guard_socket_path),
        compression=None,
        max_size=None,
    ):
        yield


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_process_group_gone(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group_id):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


async def _terminate_isolated_process_tree(
    server: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 5,
) -> None:
    """Stop the dedicated probe process group without touching local services."""

    process_group_id = server.pid
    parent_wait = (
        asyncio.create_task(server.wait(), name="probe-app-server-parent-wait")
        if server.returncode is None
        else None
    )
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if await _wait_process_group_gone(process_group_id, grace_seconds):
        if parent_wait is not None:
            await parent_wait
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if parent_wait is not None:
        await parent_wait
    if not await _wait_process_group_gone(process_group_id, 2):
        raise ProbeFailure("isolated App Server process group did not terminate")


async def _run_protocol_probe(
    args: argparse.Namespace,
    *,
    socket_path: Path,
    workspace: Path,
    env: dict[str, str],
    version: str,
) -> JSON:
    if args.approval_comparison:
        return await _run_approval_comparison(
            args,
            socket_path=socket_path,
            workspace=workspace,
            env=env,
            version=version,
        )

    clients: list[JsonlClient] = []

    async def connect(name: str) -> JsonlClient:
        client = await JsonlClient.connect(
            name,
            shim_path=Path(__file__).resolve(),
            shim_bin=args.shim_bin,
            socket_path=socket_path,
            env=env,
            t3_chat_sync_shape=(
                args.t3_chat_sync_shape and name.startswith("client-b")
            ),
        )
        clients.append(client)
        return client

    try:
        _stage("connect-two-jsonl-shims")
        a = await connect("client-a-im")
        b = await connect("client-b-t3")
        await asyncio.gather(_initialize(a), _initialize(b))
        reload_summary: JSON | None = None
        if args.t3_chat_sync_shape:
            reload_result = await b.request("config/mcpServer/reload")
            if reload_result != {}:
                raise ProbeFailure("T3 chat-sync MCP reload did not return an empty result")
            reload_summary = {"localResponse": True, "forwardedToNative": False}
        _stage("same-thread-resume")
        thread = await _start_thread(a, workspace)
        thread_id = str(thread["id"])
        await _bootstrap_persisted_thread(a, thread_id)
        resumed = await _resume(b, thread_id)
        if str(resumed["id"]) != thread_id:
            raise ProbeFailure("two clients did not resume the same native thread")

        if args.only_conflict:
            _stage("concurrent-start-conflict")
            return {
                "status": "PASS",
                "version": version,
                "conflict": await _conflict_probe(a, b, thread_id),
                "productionLocalEndpointsUsed": False,
            }

        _stage("im-to-t3-projection")
        projection_a = await _run_tool_turn(a, b, thread_id, "FROM_IM_DONE")
        _stage("t3-to-im-projection")
        projection_b = await _run_tool_turn(b, a, thread_id, "FROM_T3_DONE")
        _stage("active-turn-reconnect")
        reconnect_summary, b = await _active_reconnect_probe(
            a, b, thread_id=thread_id, reconnect=connect
        )
        _stage("approval-replay-resolution")
        approval_summary, b = await _approval_probe(a, b, cwd=workspace, reconnect=connect)
        await _resume(b, thread_id)
        conflict_summary: dict[str, Any] | None = None
        if not args.sequential_contract:
            _stage("concurrent-start-conflict")
            conflict_summary = await _conflict_probe(a, b, thread_id)
        return {
            "status": "PASS",
            "version": version,
            "sameThreadResume": thread_id,
            "bidirectional": [projection_a, projection_b],
            "activeReconnect": reconnect_summary,
            "approval": approval_summary,
            "conflict": conflict_summary,
            "contract": "sequential-v1" if args.sequential_contract else "concurrent-safe",
            "productionLocalEndpointsUsed": False,
            "shim": "product" if args.shim_bin else "embedded-probe",
            "t3McpReload": reload_summary,
        }
    finally:
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


async def _run_approval_comparison(
    args: argparse.Namespace,
    *,
    socket_path: Path,
    workspace: Path,
    env: dict[str, str],
    version: str,
) -> JSON:
    outcomes: dict[str, JSON] = {}
    variants = (("embedded-probe", None), ("product", args.shim_bin))
    for label, shim_bin in variants:
        clients: list[JsonlClient] = []

        async def connect(name: str) -> JsonlClient:
            client = await JsonlClient.connect(
                f"{label}-{name}",
                shim_path=Path(__file__).resolve(),
                shim_bin=shim_bin,
                socket_path=socket_path,
                env=env,
                t3_chat_sync_shape=False,
            )
            clients.append(client)
            return client

        _stage(f"approval-comparison-{label}")
        try:
            a = await connect("a")
            b = await connect("b")
            await asyncio.gather(_initialize(a), _initialize(b))
            summary, _ = await _approval_probe(
                a,
                b,
                cwd=workspace,
                reconnect=connect,
                deterministic=args.deterministic_approval,
            )
            outcomes[label] = {"status": "PASS", "approval": summary}
        except ProbeFailure as exc:
            outcomes[label] = {
                "status": "FAIL",
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        finally:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)

    if any(outcome["status"] != "PASS" for outcome in outcomes.values()):
        raise ProbeFailure(
            "approval comparison failed: "
            + json.dumps(outcomes, separators=(",", ":"), sort_keys=True)
        )
    return {
        "status": "PASS",
        "version": version,
        "approvalComparison": outcomes,
        "deterministicApproval": args.deterministic_approval,
        "productionLocalEndpointsUsed": False,
    }


async def _run_writer_lock_recovery_probe(
    args: argparse.Namespace,
    *,
    codex_bin: Path,
    primary_socket_path: Path,
    secondary_socket_path: Path,
    workspace: Path,
    env: dict[str, str],
    version: str,
) -> JSON:
    """Verify that a competing App Server can resume only after native lock release."""

    primary: JsonlClient | None = None
    secondary: JsonlClient | None = None
    primary_server = contextlib.AsyncExitStack()
    secondary_server = contextlib.AsyncExitStack()
    secondary_env = dict(env)
    secondary_sqlite_home = secondary_socket_path.parent / "secondary-sqlite"
    secondary_sqlite_home.mkdir(mode=0o700)
    secondary_env["CODEX_SQLITE_HOME"] = str(secondary_sqlite_home)
    thread_id = ""
    try:
        _stage("writer-lock-primary")
        await primary_server.enter_async_context(
            _isolated_app_server(
                codex_bin,
                socket_path=primary_socket_path,
                workspace=workspace,
                env=env,
            )
        )
        primary = await JsonlClient.connect(
            "writer-primary",
            shim_path=Path(__file__).resolve(),
            shim_bin=args.shim_bin,
            socket_path=primary_socket_path,
            env=env,
        )
        await _initialize(primary)
        thread = await _start_thread(primary, workspace)
        thread_id = str(thread["id"])
        await _bootstrap_persisted_thread(primary, thread_id)

        _stage("writer-lock-conflict")
        await secondary_server.enter_async_context(
            _isolated_app_server(
                codex_bin,
                socket_path=secondary_socket_path,
                workspace=workspace,
                env=secondary_env,
            )
        )
        secondary = await JsonlClient.connect(
            "writer-secondary",
            shim_path=Path(__file__).resolve(),
            shim_bin=args.shim_bin,
            socket_path=secondary_socket_path,
            env=secondary_env,
        )
        await _initialize(secondary)
        read_result = await secondary.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        if str(_thread_from(read_result)["id"]) != thread_id:
            raise ProbeFailure("competing App Server read returned a different thread")
        conflict = await secondary.request_result(
            "thread/resume",
            {"threadId": thread_id},
        )
        error = conflict.get("error")
        if not isinstance(error, dict) or error.get("code") != -32600:
            raise ProbeFailure("competing App Server resume did not return native conflict")
        expected_conflict = f"thread {thread_id} already has an active writer".lower()
        if str(error.get("message") or "").strip().lower() != expected_conflict:
            raise ProbeFailure("competing App Server resume returned an unexpected conflict")

        await primary.close()
        primary = None
        # Leave the secondary process and connection alive while the primary
        # App Server exits and releases the native writer lock.
        await primary_server.aclose()

        if secondary is None:
            raise ProbeFailure("secondary App Server client was not created")
        _stage("writer-lock-released-retry")
        resumed = await _resume(secondary, thread_id)
        if str(resumed["id"]) != thread_id:
            raise ProbeFailure("post-release retry resumed a different thread")
        loaded = await secondary.request("thread/loaded/list", {})
        loaded_ids = loaded.get("data")
        if not isinstance(loaded_ids, list) or thread_id not in {str(item) for item in loaded_ids}:
            raise ProbeFailure("post-release retry did not load the native thread")
        return {
            "status": "PASS",
            "version": version,
            "writerConflict": {
                "threadId": thread_id,
                "readWithoutWriter": True,
                "resumeConflictCode": -32600,
                "resumedAfterRelease": True,
            },
            "productionLocalEndpointsUsed": False,
        }
    finally:
        clients = [client for client in (primary, secondary) if client is not None]
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        await secondary_server.aclose()
        await primary_server.aclose()


def _scrubbed_environment(root: Path, codex_home: Path) -> dict[str, str]:
    env = {
        key: value
        for key in ("PATH", "LANG", "LC_ALL", "SHELL", "USER")
        if (value := os.environ.get(key))
    }
    isolated_home = root / "home"
    isolated_home.mkdir(mode=0o700)
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "HOME": str(isolated_home),
            "TMPDIR": str(root),
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    return env


async def run(args: argparse.Namespace) -> JSON:
    codex_bin = args.codex_bin.resolve()
    auth_source = args.auth_source.expanduser().resolve()
    if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
        raise ProbeFailure("--codex-bin must be an executable file")
    if not auth_source.is_file():
        raise ProbeFailure("--auth-source does not exist")
    if args.shim_bin is not None:
        args.shim_bin = args.shim_bin.resolve()
        if not args.shim_bin.is_file() or not os.access(args.shim_bin, os.X_OK):
            raise ProbeFailure("--shim-bin must be an executable file")

    # macOS Unix-domain socket paths are short; /tmp avoids the long per-user
    # TMPDIR prefix exceeding the native control socket limit.
    with tempfile.TemporaryDirectory(prefix="imcodex-shared-probe-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(stat.S_IRWXU)
        codex_home = root / "codex-home"
        codex_home.mkdir(mode=0o700)
        auth_target = codex_home / "auth.json"
        shutil.copyfile(auth_source, auth_target)
        auth_target.chmod(0o600)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        native_socket_path = root / "app-server.sock"
        secondary_socket_path = root / "app-server-secondary.sock"
        guard_socket_path = root / "reload-guard.sock"
        env = _scrubbed_environment(root, codex_home)

        version_process = await asyncio.create_subprocess_exec(
            str(codex_bin),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        version_out, _ = await version_process.communicate()
        version = version_out.decode(errors="replace").strip()
        if version_process.returncode != 0 or version != args.expected_version:
            raise ProbeFailure(f"version mismatch: expected={args.expected_version!r} actual={version!r}")

        if args.writer_lock_recovery:
            return await _run_writer_lock_recovery_probe(
                args,
                codex_bin=codex_bin,
                primary_socket_path=native_socket_path,
                secondary_socket_path=secondary_socket_path,
                workspace=workspace,
                env=env,
                version=version,
            )

        async with _isolated_app_server(
            codex_bin,
            socket_path=native_socket_path,
            workspace=workspace,
            env=env,
        ):
            if args.t3_chat_sync_shape:
                forwarded_reload: list[bool] = []
                async with _reload_guard(
                    native_socket_path,
                    guard_socket_path,
                    forwarded_reload,
                ):
                    try:
                        return await _run_protocol_probe(
                            args,
                            socket_path=guard_socket_path,
                            workspace=workspace,
                            env=env,
                            version=version,
                        )
                    finally:
                        if forwarded_reload:
                            raise ProbeFailure("T3 MCP reload reached the native-server guard")
            return await _run_protocol_probe(
                args,
                socket_path=native_socket_path,
                workspace=workspace,
                env=env,
                version=version,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--auth-source", type=Path, default=Path.home() / ".codex" / "auth.json")
    parser.add_argument("--expected-version", default="codex-cli 0.144.1")
    parser.add_argument(
        "--shim-bin",
        type=Path,
        help="Use the product stdio-to-shared-server shim instead of the probe's embedded shim.",
    )
    parser.add_argument(
        "--approval-comparison",
        action="store_true",
        help="Run the same approval gate through embedded and product relays on one isolated server.",
    )
    parser.add_argument(
        "--deterministic-approval",
        action="store_true",
        help="Use a declined read-only-sandbox write attempt for the approval comparison.",
    )
    parser.add_argument(
        "--t3-chat-sync-shape",
        action="store_true",
        help="Launch the T3-shaped client with safe chat-sync args and guard native MCP reload.",
    )
    contract = parser.add_mutually_exclusive_group()
    contract.add_argument("--only-conflict", action="store_true")
    contract.add_argument(
        "--writer-lock-recovery",
        action="store_true",
        help=(
            "Verify native cross-process writer conflict and successful resume "
            "after the owning App Server exits."
        ),
    )
    contract.add_argument(
        "--sequential-contract",
        action="store_true",
        help="Verify the v1 sequential two-client contract without requiring concurrent turn/start rejection.",
    )
    args = parser.parse_args()
    if args.approval_comparison and args.shim_bin is None:
        parser.error("--approval-comparison requires --shim-bin")
    if args.deterministic_approval and not args.approval_comparison:
        parser.error("--deterministic-approval requires --approval-comparison")
    if args.t3_chat_sync_shape and args.shim_bin is None:
        parser.error("--t3-chat-sync-shape requires --shim-bin")
    if args.t3_chat_sync_shape and args.approval_comparison:
        parser.error("--t3-chat-sync-shape is a full sequential probe option")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAIL", "errorType": type(exc).__name__, "error": str(exc)},
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["app-server"]:
        raise SystemExit(asyncio.run(_stdio_shim()))
    raise SystemExit(main())

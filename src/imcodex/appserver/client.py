from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..observability.runtime import emit_event, mark_appserver_health


JsonDict = dict[str, Any]
NotificationHandler = Callable[[JsonDict], Awaitable[None] | None]
ServerRequestHandler = Callable[[JsonDict], Awaitable[None] | None]
ConnectionResetHandler = Callable[[int], Awaitable[None] | None]
ConnectionReadyHandler = Callable[[int], Awaitable[None] | None]
_TRIMMED_THREAD_METHODS = frozenset({"thread/resume", "thread/fork", "thread/rollback"})
_MAX_RECENT_THREAD_TURNS = 4


class AppServerError(RuntimeError):
    pass


class AppServerTransport(Protocol):
    async def send_json(self, payload: JsonDict) -> None: ...

    async def receive_json(self) -> JsonDict: ...

    async def close(self) -> None: ...

    def is_closed(self) -> bool: ...


class StdioAppServerTransport:
    def __init__(self, process: Any) -> None:
        self._process = process
        self._stdin = getattr(process, "stdin", None)
        self._stdout = getattr(process, "stdout", None)
        self._buffer = bytearray()
        if self._stdin is None or self._stdout is None:
            raise AppServerError("stdio app-server process missing stdin/stdout pipes")

    async def send_json(self, payload: JsonDict) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self._stdin.write(data)
        drain = getattr(self._stdin, "drain", None)
        if callable(drain):
            result = drain()
            if inspect.isawaitable(result):
                await result

    async def receive_json(self) -> JsonDict:
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index >= 0:
                raw = bytes(self._buffer[:newline_index])
                del self._buffer[: newline_index + 1]
                if not raw.strip():
                    continue
                return json.loads(raw.decode("utf-8"))
            chunk = await self._read_chunk()
            if not chunk:
                if not self._buffer:
                    raise AppServerError("app-server connection closed")
                raw = bytes(self._buffer)
                self._buffer.clear()
                if not raw.strip():
                    raise AppServerError("app-server connection closed")
                return json.loads(raw.decode("utf-8"))
            self._buffer.extend(chunk)

    async def close(self) -> None:
        close = getattr(self._stdin, "close", None)
        if callable(close):
            close()

    def is_closed(self) -> bool:
        return getattr(self._process, "returncode", None) is not None

    async def _read_chunk(self) -> bytes:
        read = getattr(self._stdout, "read", None)
        if callable(read):
            chunk = read(65536)
            if inspect.isawaitable(chunk):
                chunk = await chunk
            return chunk
        raw = await self._stdout.readline()
        return raw


class WebSocketAppServerTransport:
    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket

    async def send_json(self, payload: JsonDict) -> None:
        await self._websocket.send(json.dumps(payload))

    async def receive_json(self) -> JsonDict:
        try:
            raw = await self._websocket.recv()
        except Exception as exc:
            raise AppServerError("app-server connection closed") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def close(self) -> None:
        close = getattr(self._websocket, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def is_closed(self) -> bool:
        return bool(getattr(self._websocket, "closed", False))


class AppServerClient:
    def __init__(
        self,
        *,
        supervisor,
        client_info: dict[str, str],
        request_timeout_s: float = 15.0,
    ) -> None:
        self._supervisor = supervisor
        self._client_info = client_info
        self._request_timeout_s = request_timeout_s
        self._transport: AppServerTransport | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._pending_futures: dict[int, asyncio.Future[JsonDict]] = {}
        self._notification_handlers: list[NotificationHandler] = []
        self._server_request_handlers: list[ServerRequestHandler] = []
        self._connection_reset_handlers: list[ConnectionResetHandler] = []
        self._connection_ready_handlers: list[ConnectionReadyHandler] = []
        self.connection_mode = "disconnected"
        self.last_connection_mode = "disconnected"
        self.connection_epoch = 0
        self.initialized = False

    async def connect(self) -> None:
        await self._ensure_connected()

    async def close(self) -> None:
        await self._reset_connection(notify_handlers=False)

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def add_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handlers.append(handler)

    def add_connection_reset_handler(self, handler: ConnectionResetHandler) -> None:
        self._connection_reset_handlers.append(handler)

    def add_connection_ready_handler(self, handler: ConnectionReadyHandler) -> None:
        self._connection_ready_handlers.append(handler)

    async def initialize(self) -> JsonDict:
        await self._ensure_connected()
        result = await self._request_without_initialize(
            "initialize",
            {
                "clientInfo": self._client_info,
                "capabilities": {
                    "optOutNotificationMethods": [
                        "command/exec/outputDelta",
                        "item/agentMessage/delta",
                        "item/plan/delta",
                        "item/commandExecution/outputDelta",
                        "item/fileChange/outputDelta",
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/textDelta",
                        "thread/realtime/transcript/delta",
                        "thread/realtime/outputAudio/delta",
                    ]
                },
            },
        )
        await self._notify("initialized", {})
        self.initialized = True
        for handler in list(self._connection_ready_handlers):
            ready = handler(self.connection_epoch)
            if inspect.isawaitable(ready):
                await ready
        return result

    async def call(self, method: str, params: JsonDict | None = None) -> JsonDict:
        return await self._request(method, dict(params or {}))

    async def start_thread(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("thread/start", self._normalize_thread_params(payload))

    async def resume_thread(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("thread/resume", self._normalize_thread_params(payload))

    async def list_threads(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("thread/list", payload)

    async def list_models(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("model/list", payload)

    async def read_thread(self, thread_id: str) -> JsonDict:
        return await self._request("thread/read", {"threadId": thread_id})

    async def read_config(self, *, include_layers: bool = False, cwd: str | None = None) -> JsonDict:
        payload: JsonDict = {"includeLayers": include_layers}
        if cwd is not None:
            payload["cwd"] = cwd
        return await self._request("config/read", payload)

    async def write_config_value(
        self,
        *,
        key_path: str,
        value: Any,
        merge_strategy: str = "replace",
    ) -> JsonDict:
        return await self._request(
            "config/value/write",
            {
                "keyPath": key_path,
                "value": value,
                "mergeStrategy": merge_strategy,
            },
        )

    async def batch_write_config(
        self,
        *,
        edits: list[JsonDict],
        reload_user_config: bool = False,
    ) -> JsonDict:
        return await self._request(
            "config/batchWrite",
            {
                "edits": edits,
                "reloadUserConfig": reload_user_config,
            },
        )

    async def start_turn(self, thread_id: str, text: str, **kwargs: Any) -> JsonDict:
        payload = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        for key in ("cwd", "model", "summary"):
            value = kwargs.get(key)
            if value is not None:
                payload[key] = value
        return await self._request("turn/start", payload)

    async def steer_turn(self, thread_id: str, turn_id: str, text: str) -> JsonDict:
        return await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> JsonDict:
        return await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def reply_to_transport_request(self, transport_request_id: str | int, result: JsonDict) -> JsonDict:
        await self._ensure_connected()
        payload = {"id": transport_request_id, "result": result}
        await self._send_json(payload)
        return payload

    async def reply_error_to_transport_request(
        self,
        transport_request_id: str | int,
        *,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> JsonDict:
        await self._ensure_connected()
        error: JsonDict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        payload = {"id": transport_request_id, "error": error}
        await self._send_json(payload)
        return payload

    async def _ensure_connected(self) -> None:
        if self._listener_task is not None and self._listener_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
            self._listener_task = None
            await self._reset_connection()
        if self._transport is not None and self._transport.is_closed():
            await self._reset_connection()
        if self._transport is None:
            emit_event(
                component="appserver.client",
                event="appserver.connect.started",
                message="Connecting to app-server",
            )
            websocket = await self._supervisor.connect_shared()
            if websocket is not None:
                self._transport = WebSocketAppServerTransport(websocket)
                self.connection_mode = "shared-ws"
                self.connection_epoch += 1
                emit_event(
                    component="appserver.client",
                    event="appserver.connect.shared_ws_succeeded",
                    message="Connected to shared websocket app-server",
                )
                mark_appserver_health(connected=True, mode="shared-ws")
            else:
                if not self._supervisor.allow_spawn_fallback:
                    target = self._supervisor.core_url or self._supervisor.app_server_url or self._supervisor.shared_app_server_url
                    raise AppServerError(f"dedicated app-server at `{target}` is unavailable")
                process = await self._supervisor.start()
                self._transport = StdioAppServerTransport(process)
                self.connection_mode = "spawned-stdio"
                self.connection_epoch += 1
                emit_event(
                    component="appserver.client",
                    event="appserver.connect.spawn_stdio_succeeded",
                    message="Connected to spawned stdio app-server",
                )
                mark_appserver_health(connected=True, mode="spawned-stdio")
            self._listener_task = asyncio.create_task(self._receive_loop())
            self.initialized = False

    async def _ensure_ready(self) -> None:
        await self._ensure_connected()
        if not self.initialized:
            await self.initialize()

    async def _request(self, method: str, params: JsonDict) -> JsonDict:
        await self._ensure_ready()
        return await self._request_without_initialize(method, params)

    async def _request_without_initialize(self, method: str, params: JsonDict) -> JsonDict:
        await self._ensure_connected()
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._pending_futures[request_id] = future
        await self._send_json({"id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=self._request_timeout_s)
        except asyncio.TimeoutError as exc:
            await self._reset_connection()
            raise AppServerError(f"{method} timed out after {self._request_timeout_s:.1f}s") from exc
        finally:
            self._pending_futures.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise AppServerError(message or f"{method} failed")
        return self._normalize_result(method, response["result"])

    async def _notify(self, method: str, params: JsonDict) -> None:
        await self._send_json({"method": method, "params": params})

    async def _send_json(self, payload: JsonDict) -> None:
        if self._transport is None:
            raise AppServerError("transport is not connected")
        await self._transport.send_json(payload)

    async def _receive_loop(self) -> None:
        error: Exception | None = None
        try:
            while self._transport is not None:
                message = await self._transport.receive_json()
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            if error is not None:
                for future in self._pending_futures.values():
                    if not future.done():
                        future.set_exception(error)
            self.initialized = False

    async def _dispatch(self, message: JsonDict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending_futures.get(int(message["id"]))
            if future is not None and not future.done():
                future.set_result(message)
            return
        if "id" in message and "method" in message:
            request_id = str(message["id"])
            enriched = {
                "id": message["id"],
                "method": message["method"],
                "params": {
                    **(message.get("params") or {}),
                    "_request_id": request_id,
                    "_transport_request_id": message["id"],
                    "_connection_epoch": self.connection_epoch,
                },
            }
            for handler in list(self._server_request_handlers):
                result = handler(enriched)
                if inspect.isawaitable(result):
                    await result
            return
        if "method" in message:
            notification = {"method": message["method"], "params": message.get("params", {})}
            for handler in list(self._notification_handlers):
                result = handler(notification)
                if inspect.isawaitable(result):
                    await result

    async def _reset_connection(self, *, notify_handlers: bool = True) -> None:
        reset_epoch = self.connection_epoch
        if self.connection_mode != "disconnected":
            self.last_connection_mode = self.connection_mode
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None and listener_task is not asyncio.current_task():
            if not listener_task.done():
                listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener_task
        transport = self._transport
        self._transport = None
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()
        self.initialized = False
        self.connection_mode = "disconnected"
        await self._supervisor.stop()
        if notify_handlers and reset_epoch > 0:
            for handler in list(self._connection_reset_handlers):
                result = handler(reset_epoch)
                if inspect.isawaitable(result):
                    await result
        if self._transport is None:
            self.connection_mode = "disconnected"
            emit_event(
                component="appserver.client",
                event="appserver.connection.closed",
                message="App-server connection closed",
            )
            mark_appserver_health(connected=False, mode="disconnected")

    def _normalize_thread_params(self, payload: JsonDict) -> JsonDict:
        mappings = {
            "thread_id": "threadId",
            "approval_policy": "approvalPolicy",
            "sandbox_policy": "sandboxPolicy",
            "approvals_reviewer": "approvalsReviewer",
            "service_name": "serviceName",
        }
        return {mappings.get(key, key): value for key, value in payload.items()}

    def _normalize_result(self, method: str, result: JsonDict) -> JsonDict:
        if method in _TRIMMED_THREAD_METHODS:
            self._trim_thread_history(result)
        return result

    def _trim_thread_history(self, result: JsonDict) -> None:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            return
        turns = thread.get("turns")
        if not isinstance(turns, list) or len(turns) <= _MAX_RECENT_THREAD_TURNS:
            return
        thread["turns"] = turns[-_MAX_RECENT_THREAD_TURNS:]

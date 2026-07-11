from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from .diagnostics import summarize_text, summarize_transport_message
from .retry import RetryBackoff
from ..observability.runtime import emit_event, mark_appserver_health


JsonDict = dict[str, Any]
NotificationHandler = Callable[[JsonDict], Awaitable[None] | None]
ServerRequestHandler = Callable[[JsonDict], Awaitable[None] | None]
ConnectionResetHandler = Callable[[int], Awaitable[None] | None]
ConnectionReadyHandler = Callable[[int], Awaitable[None] | None]
_TRIMMED_THREAD_METHODS = frozenset({"thread/resume", "thread/fork", "thread/rollback"})
_MAX_RECENT_THREAD_TURNS = 4
DEFAULT_OPT_OUT_NOTIFICATION_METHODS = (
    "account/rateLimits/updated",
    "command/exec/outputDelta",
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "thread/realtime/transcript/delta",
    "thread/realtime/outputAudio/delta",
)
_WEBSOCKET_CONNECTION_MODES = frozenset({"dedicated-ws", "shared-ws"})


class AppServerError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


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
        experimental_api_enabled: bool = False,
        request_timeout_s: float = 15.0,
        request_retry_policy: RetryBackoff | None = None,
        reconnect_retry_policy: RetryBackoff | None = None,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
        random_float: Callable[[], float] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._client_info = client_info
        self._experimental_api_enabled = experimental_api_enabled
        self._request_timeout_s = request_timeout_s
        self._request_retry_policy = request_retry_policy or RetryBackoff()
        self._reconnect_retry_policy = reconnect_retry_policy or RetryBackoff()
        self._sleep = sleep or asyncio.sleep
        self._random_float = random_float
        self._transport: AppServerTransport | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._dispatch_queue: asyncio.Queue[JsonDict] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._resetting = False
        self._reset_owner_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._initialize_lock = asyncio.Lock()
        self._initialize_lock_owner_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False
        self._protocol_initialized = False
        self._initialize_owner_task: asyncio.Task | None = None
        self._initialize_owner_epoch: int | None = None
        self._initialize_owner_transport: AppServerTransport | None = None
        self._initialize_result: JsonDict | None = None
        self._has_been_ready = False
        self._next_request_id = 1
        self._pending_futures: dict[int, tuple[int, asyncio.Future[JsonDict]]] = {}
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
        self._closing = True
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reconnect_task
        async with self._connect_lock:
            await self._reset_connection(notify_handlers=False)

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def add_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handlers.append(handler)

    def add_connection_reset_handler(self, handler: ConnectionResetHandler) -> None:
        self._connection_reset_handlers.append(handler)

    def add_connection_ready_handler(self, handler: ConnectionReadyHandler) -> None:
        self._connection_ready_handlers.append(handler)

    @contextlib.asynccontextmanager
    async def _owned_initialize_lock(self) -> AsyncIterator[None]:
        async with self._initialize_lock:
            current_task = asyncio.current_task()
            self._initialize_lock_owner_task = current_task
            try:
                yield
            finally:
                if self._initialize_lock_owner_task is current_task:
                    self._initialize_lock_owner_task = None

    async def initialize(self) -> JsonDict:
        async with self._owned_initialize_lock():
            if self._closing:
                raise AppServerError("app-server client is closed")
            if self.initialized:
                return dict(self._initialize_result or {})
            await self._ensure_connected()
            initialize_epoch = self.connection_epoch
            initialize_transport = self._transport
            if initialize_transport is None:
                raise AppServerError("transport is not connected")
            if not self._protocol_initialized:
                try:
                    result = await self._request_without_initialize(
                        "initialize",
                        {
                            "clientInfo": self._client_info,
                            "capabilities": self._initialize_capabilities(),
                        },
                    )
                    await self._send_json(
                        {"method": "initialized", "params": {}},
                        transport=initialize_transport,
                    )
                    self._require_initializing_connection(
                        initialize_epoch,
                        initialize_transport,
                        require_protocol_initialized=False,
                    )
                except BaseException:
                    if self.connection_epoch == initialize_epoch and self._transport is initialize_transport:
                        await self._reset_connection()
                    raise
                self._protocol_initialized = True
                self._initialize_result = dict(result)
            else:
                result = dict(self._initialize_result or {})
            current_task = asyncio.current_task()
            self._initialize_owner_task = current_task
            self._initialize_owner_epoch = initialize_epoch
            self._initialize_owner_transport = initialize_transport
            try:
                for handler in list(self._connection_ready_handlers):
                    self._require_initializing_connection(initialize_epoch, initialize_transport)
                    ready = handler(initialize_epoch)
                    if inspect.isawaitable(ready):
                        await ready
                self._require_initializing_connection(initialize_epoch, initialize_transport)
            except asyncio.CancelledError:
                self.initialized = False
                if self.connection_epoch == initialize_epoch and self._transport is initialize_transport:
                    await self._reset_connection()
                raise
            except Exception:
                self.initialized = False
                raise
            finally:
                if self._initialize_owner_task is current_task:
                    self._initialize_owner_task = None
                    self._initialize_owner_epoch = None
                    self._initialize_owner_transport = None
            self.initialized = True
            self._has_been_ready = True
            mark_appserver_health(
                connected=True,
                mode=self.connection_mode,
                status="connected",
                retry_attempt=None,
                retry_delay_s=None,
                error_type=None,
                health_ok=None,
                health_status_code=None,
                health_error_type=None,
            )
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

    async def fork_thread(self, thread_id: str) -> JsonDict:
        return await self._request("thread/fork", {"threadId": thread_id})

    async def set_thread_name(self, thread_id: str, name: str) -> JsonDict:
        return await self._request("thread/name/set", {"threadId": thread_id, "name": name})

    async def compact_thread(self, thread_id: str) -> JsonDict:
        return await self._request("thread/compact/start", {"threadId": thread_id})

    async def list_models(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("model/list", payload)

    async def list_permission_profiles(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        payload = dict(params or {})
        payload.update(kwargs)
        return await self._request("permissionProfile/list", payload)

    async def read_config_requirements(self) -> JsonDict:
        return await self._request("configRequirements/read", None)

    async def read_account_rate_limits(self) -> JsonDict:
        return await self._request("account/rateLimits/read", None)

    async def read_account_usage(self) -> JsonDict:
        return await self._request("account/usage/read", None)

    async def read_thread(self, thread_id: str, *, include_turns: bool = False) -> JsonDict:
        payload: JsonDict = {"threadId": thread_id}
        if include_turns:
            payload["includeTurns"] = True
        return await self._request("thread/read", payload)

    async def list_thread_turns(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> JsonDict:
        payload: JsonDict = {"threadId": thread_id}
        if limit is not None:
            payload["limit"] = limit
        if cursor is not None:
            payload["cursor"] = cursor
        return await self._request("thread/turns/list", payload)

    async def get_thread_goal(self, thread_id: str) -> JsonDict:
        return await self._request("thread/goal/get", {"threadId": thread_id})

    async def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> JsonDict:
        payload: JsonDict = {"threadId": thread_id}
        if objective is not None:
            payload["objective"] = objective
        if status is not None:
            payload["status"] = status
        if token_budget is not None:
            payload["tokenBudget"] = token_budget
        return await self._request("thread/goal/set", payload)

    async def clear_thread_goal(self, thread_id: str) -> JsonDict:
        return await self._request("thread/goal/clear", {"threadId": thread_id})

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
        expected_version: str | None = None,
        file_path: str | None = None,
    ) -> JsonDict:
        payload: JsonDict = {
            "edits": edits,
            "reloadUserConfig": reload_user_config,
        }
        if expected_version is not None:
            payload["expectedVersion"] = expected_version
        if file_path is not None:
            payload["filePath"] = file_path
        return await self._request(
            "config/batchWrite",
            payload,
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

    async def reply_to_transport_request(
        self,
        transport_request_id: str | int,
        result: JsonDict,
        *,
        expected_connection_epoch: int | None = None,
    ) -> JsonDict:
        await self._ensure_connected()
        transport = self._reply_transport(expected_connection_epoch)
        reply_epoch = self.connection_epoch
        payload = {"id": transport_request_id, "result": result}
        try:
            await self._send_json(payload, transport=transport)
        except Exception:
            if self.connection_epoch == reply_epoch and self._transport is transport:
                await self._reset_connection()
            raise
        return payload

    async def reply_error_to_transport_request(
        self,
        transport_request_id: str | int,
        *,
        code: int,
        message: str,
        data: Any | None = None,
        expected_connection_epoch: int | None = None,
    ) -> JsonDict:
        await self._ensure_connected()
        transport = self._reply_transport(expected_connection_epoch)
        reply_epoch = self.connection_epoch
        error: JsonDict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        payload = {"id": transport_request_id, "error": error}
        try:
            await self._send_json(payload, transport=transport)
        except Exception:
            if self.connection_epoch == reply_epoch and self._transport is transport:
                await self._reset_connection()
            raise
        return payload

    def _reply_transport(self, expected_connection_epoch: int | None) -> AppServerTransport:
        transport = self._transport
        if transport is None:
            raise AppServerError("transport is not connected")
        if (
            expected_connection_epoch is not None
            and expected_connection_epoch > 0
            and self.connection_epoch != expected_connection_epoch
        ):
            raise AppServerError(
                "server request belongs to an expired app-server connection "
                f"(expected epoch {expected_connection_epoch}, current epoch {self.connection_epoch})"
            )
        return transport

    async def _ensure_connected(self) -> None:
        current_task = asyncio.current_task()
        if (
            self._resetting
            and self._reset_owner_task is current_task
            and self._initialize_lock.locked()
            and self._initialize_lock_owner_task is not current_task
        ):
            raise AppServerError("app-server connection reset during initialization")
        if self._initialize_owner_task is current_task:
            owner_epoch = self._initialize_owner_epoch
            owner_transport = self._initialize_owner_transport
            if owner_epoch is None or owner_transport is None:
                raise AppServerError("app-server initialization context is unavailable")
            self._require_initializing_connection(owner_epoch, owner_transport)
            return
        while True:
            if self._closing:
                raise AppServerError("app-server client is closed")
            await self._wait_for_connection_reset()
            listener_task = self._listener_task
            if listener_task is not None and listener_task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await listener_task
                if self._listener_task is listener_task:
                    self._listener_task = None
                await self._reset_connection()
                continue
            if self._transport is not None and self._transport.is_closed():
                await self._reset_connection()
                continue
            if self._transport is not None:
                return
            async with self._connect_lock:
                await self._wait_for_connection_reset()
                if self._closing:
                    raise AppServerError("app-server client is closed")
                if self._transport is not None:
                    if self._transport.is_closed():
                        continue
                    return
                await self._open_connection()
                return

    async def _open_connection(self) -> None:
        emit_event(
            component="appserver.client",
            event="appserver.connect.started",
            message="Connecting to app-server",
        )
        try:
            websocket = await self._supervisor.connect_shared()
        except Exception as exc:
            emit_event(
                component="appserver.client",
                event="appserver.connect.failed",
                level="ERROR",
                message="Failed to prepare app-server connection",
                data={"error_type": type(exc).__name__},
            )
            mark_appserver_health(connected=False, mode="disconnected", error_type=type(exc).__name__)
            raise AppServerError(str(exc) or "failed to prepare app-server connection") from exc
        if self._closing:
            if websocket is not None:
                close = getattr(websocket, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        with contextlib.suppress(Exception):
                            await result
            raise AppServerError("app-server client is closed")
        if websocket is not None:
            transport: AppServerTransport = WebSocketAppServerTransport(websocket)
            websocket_mode = self._supervisor.connection_mode or "shared-ws"
            self.connection_mode = websocket_mode
            emit_event(
                component="appserver.client",
                event="appserver.connect.websocket_succeeded",
                message=f"Connected to {websocket_mode} app-server",
            )
            mark_appserver_health(
                connected=True,
                mode=websocket_mode,
                status="initializing",
                error_type=None,
                health_ok=None,
                health_status_code=None,
                health_error_type=None,
            )
        else:
            if not self._supervisor.allow_spawn_fallback:
                target = self._connection_target()
                diagnostic = getattr(self._supervisor, "last_connect_diagnostic", None) or {}
                display_target = str(diagnostic.get("url") or target)
                mark_appserver_health(
                    connected=False,
                    mode="disconnected",
                    target=display_target,
                    error_type=diagnostic.get("error_type"),
                    health_ok=diagnostic.get("health_ok"),
                )
                raise AppServerError(self._unavailable_message(display_target, diagnostic))
            process = await self._supervisor.start()
            transport = StdioAppServerTransport(process)
            self.connection_mode = "spawned-stdio"
            self._stderr_task = asyncio.create_task(self._drain_process_stderr(process))
            emit_event(
                component="appserver.client",
                event="appserver.connect.spawn_stdio_succeeded",
                message="Connected to spawned stdio app-server",
            )
            if self._closing:
                await self._supervisor.stop()
                raise AppServerError("app-server client is closed")
            mark_appserver_health(
                connected=True,
                mode="spawned-stdio",
                status="initializing",
                error_type=None,
                health_ok=None,
                health_status_code=None,
                health_error_type=None,
            )
        self.connection_epoch += 1
        epoch = self.connection_epoch
        queue: asyncio.Queue[JsonDict] = asyncio.Queue()
        self._transport = transport
        self._dispatch_queue = queue
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop(queue, epoch))
        self._listener_task = asyncio.create_task(self._receive_loop(transport, queue, epoch))
        self._protocol_initialized = False
        self.initialized = False
        self._initialize_result = None

    async def _ensure_ready(self) -> None:
        current_task = asyncio.current_task()
        if self._initialize_owner_task is current_task:
            owner_epoch = self._initialize_owner_epoch
            owner_transport = self._initialize_owner_transport
            if owner_epoch is None or owner_transport is None:
                raise AppServerError("app-server initialization context is unavailable")
            self._require_initializing_connection(owner_epoch, owner_transport)
            return
        await self._ensure_connected()
        if self.initialized:
            return
        if not self.initialized:
            await self.initialize()

    def _require_initializing_connection(
        self,
        epoch: int,
        transport: AppServerTransport,
        *,
        require_protocol_initialized: bool = True,
    ) -> None:
        if self._closing:
            raise AppServerError("app-server client is closed")
        current_task = asyncio.current_task()
        if self._resetting and self._reset_owner_task is not current_task:
            raise AppServerError("app-server connection reset during initialization")
        if (
            self.connection_epoch != epoch
            or self._transport is not transport
            or (require_protocol_initialized and not self._protocol_initialized)
        ):
            raise AppServerError("app-server connection changed during initialization")

    async def _request(self, method: str, params: JsonDict | None) -> JsonDict:
        await self._ensure_ready()
        return await self._request_without_initialize(method, params)

    async def _request_without_initialize(self, method: str, params: JsonDict | None) -> JsonDict:
        await self._ensure_connected()
        request_epoch = self.connection_epoch
        request_transport = self._transport
        if request_transport is None:
            raise AppServerError("transport is not connected")
        attempts = self._request_retry_policy.attempts
        attempt = 1
        while True:
            if self.connection_epoch != request_epoch or self._transport is not request_transport:
                raise AppServerError(
                    f"{method} retry was cancelled because the app-server connection changed"
                )
            response = await self._request_once_without_initialize(method, params)
            if "error" not in response:
                return self._normalize_result(method, response["result"])
            error = response["error"]
            if self._is_overload_error(error) and attempt < attempts:
                delay_s = self._request_retry_policy.delay_after_failure(
                    attempt,
                    random_float=self._random_float,
                )
                emit_event(
                    component="appserver.client",
                    event="appserver.request.overload_retry_scheduled",
                    level="WARNING",
                    message="App-server overloaded; retrying request",
                    data={
                        "method": method,
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                        "delay_s": round(delay_s, 3),
                    },
                )
                await self._sleep_for_retry(delay_s)
                attempt += 1
                continue
            raise self._error_from_response(method, error)

    async def _request_once_without_initialize(self, method: str, params: JsonDict | None) -> JsonDict:
        request_id = self._next_request_id
        self._next_request_id += 1
        request_epoch = self.connection_epoch
        transport = self._transport
        if transport is None:
            raise AppServerError("transport is not connected")
        future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._pending_futures[request_id] = (request_epoch, future)
        payload: JsonDict = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send_json(payload, transport=transport)
            response = await asyncio.wait_for(future, timeout=self._request_timeout_s)
        except asyncio.TimeoutError as exc:
            if self.connection_epoch == request_epoch and self._transport is transport:
                await self._reset_connection()
            raise AppServerError(f"{method} timed out after {self._request_timeout_s:.1f}s") from exc
        except Exception:
            if not future.done():
                future.cancel()
            if self.connection_epoch == request_epoch and self._transport is transport:
                await self._reset_connection()
            raise
        finally:
            self._pending_futures.pop(request_id, None)
        return response

    def _is_overload_error(self, error: Any) -> bool:
        if not isinstance(error, dict):
            return False
        code = self._error_code(error)
        message = error.get("message")
        return code == -32001 or message == "Server overloaded; retry later"

    def _error_from_response(self, method: str, error: Any) -> AppServerError:
        if not isinstance(error, dict):
            return AppServerError(str(error) or f"{method} failed")
        message = error.get("message") or f"{method} failed"
        return AppServerError(str(message), code=self._error_code(error), data=error.get("data"))

    def _error_code(self, error: JsonDict) -> int | None:
        code = error.get("code")
        try:
            return int(code)
        except (TypeError, ValueError):
            return None

    async def _sleep_for_retry(self, delay_s: float) -> None:
        result = self._sleep(max(0.0, delay_s))
        if inspect.isawaitable(result):
            await result

    def _connection_target(self) -> str:
        return self._supervisor.core_url or self._supervisor.app_server_url or self._supervisor.shared_app_server_url

    def _unavailable_message(self, target: str, diagnostic: dict[str, Any]) -> str:
        mode = getattr(self._supervisor, "core_mode", "")
        label = "shared" if mode == "shared-ws" else "dedicated"
        detail_parts: list[str] = []
        error_type = diagnostic.get("error_type")
        if error_type:
            detail_parts.append(f"last_error={error_type}")
        health_status = diagnostic.get("health_status_code")
        if health_status is not None:
            detail_parts.append(f"health_status={health_status}")
        elif diagnostic.get("health_error_type"):
            detail_parts.append(f"health_error={diagnostic['health_error_type']}")
        details = f" ({', '.join(detail_parts)})" if detail_parts else ""
        return f"{label} app-server at `{target}` is unavailable{details}"

    async def _send_json(self, payload: JsonDict, *, transport: AppServerTransport | None = None) -> None:
        selected_transport = transport or self._transport
        if selected_transport is None:
            raise AppServerError("transport is not connected")
        if transport is not None and self._transport is not transport:
            raise AppServerError("app-server connection changed before request could be sent")
        self._trace_protocol_message(stage="sent", payload=payload)
        await selected_transport.send_json(payload)

    async def _receive_loop(
        self,
        transport: AppServerTransport,
        queue: asyncio.Queue[JsonDict],
        epoch: int,
    ) -> None:
        error: Exception | None = None
        cancelled = False
        try:
            while self._transport is transport and self.connection_epoch == epoch:
                message = await transport.receive_json()
                self._trace_protocol_message(stage="received", payload=message)
                if self._dispatch_response(message, epoch):
                    continue
                queue.put_nowait(message)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            error = exc
        finally:
            if error is not None:
                self._fail_pending_futures(epoch, error)
            if self.connection_epoch == epoch and self._transport is transport:
                self._protocol_initialized = False
                self.initialized = False
            if (
                error is not None
                and not cancelled
                and self.connection_epoch == epoch
                and self._transport is transport
            ):
                await self._reset_connection()

    async def _dispatch_loop(self, queue: asyncio.Queue[JsonDict], epoch: int) -> None:
        current_task = asyncio.current_task()
        while (
            self._dispatch_queue is queue
            and self.connection_epoch == epoch
            and self._dispatcher_task is current_task
        ):
            message = await queue.get()
            try:
                await self._dispatch(message, epoch)
            except Exception as exc:
                emit_event(
                    component="appserver.client",
                    event="appserver.dispatch.failed",
                    level="ERROR",
                    message=str(exc),
                    data={"error_type": type(exc).__name__},
                )
            finally:
                queue.task_done()

    def _dispatch_response(self, message: JsonDict, epoch: int) -> bool:
        if "id" not in message or ("result" not in message and "error" not in message):
            return False
        pending = self._pending_futures.get(int(message["id"]))
        if pending is None:
            return True
        pending_epoch, future = pending
        if pending_epoch == epoch and not future.done():
            future.set_result(message)
        return True

    async def _dispatch(self, message: JsonDict, epoch: int) -> None:
        if "id" in message and "method" in message:
            request_id = str(message["id"])
            params = message.get("params")
            if isinstance(params, dict):
                request_params = dict(params)
            elif params is None:
                request_params = {}
            else:
                request_params = {"_raw_params": params}
            enriched = {
                "id": message["id"],
                "method": message["method"],
                "params": {
                    **request_params,
                    "_request_id": request_id,
                    "_transport_request_id": message["id"],
                    "_connection_epoch": epoch,
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

    def _fail_pending_futures(self, epoch: int, error: Exception) -> None:
        for pending_epoch, future in self._pending_futures.values():
            if pending_epoch == epoch and not future.done():
                future.set_exception(error)

    async def _reset_connection(self, *, notify_handlers: bool = True) -> None:
        initiating_task = asyncio.current_task()
        if self._resetting:
            if self._reset_owner_task is initiating_task:
                return
            await self._wait_for_connection_reset()
            return
        self._resetting = True
        cleanup_task = asyncio.create_task(
            self._reset_connection_impl(
                notify_handlers=notify_handlers,
                initiating_task=initiating_task,
            )
        )
        self._reset_owner_task = cleanup_task
        cancelled = False
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            try:
                cleanup_task.result()
            except Exception:
                if not cancelled:
                    raise
        finally:
            if self._reset_owner_task is cleanup_task:
                self._resetting = False
                self._reset_owner_task = None
        if cancelled:
            raise asyncio.CancelledError

    async def _reset_connection_impl(
        self,
        *,
        notify_handlers: bool,
        initiating_task: asyncio.Task | None,
    ) -> None:
        reset_epoch = self.connection_epoch
        reset_mode = self.connection_mode
        should_reconnect = (
            notify_handlers
            and self._has_been_ready
            and reset_epoch > 0
            and reset_mode in _WEBSOCKET_CONNECTION_MODES
            and bool(getattr(self._supervisor, "supports_background_reconnect", False))
            and not self._closing
        )
        if self.connection_mode != "disconnected":
            self.last_connection_mode = self.connection_mode
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None and listener_task is not initiating_task:
            if not listener_task.done():
                listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener_task
        dispatcher_task = self._dispatcher_task
        self._dispatcher_task = None
        self._dispatch_queue = None
        if dispatcher_task is not None and dispatcher_task is not initiating_task:
            if not dispatcher_task.done():
                dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dispatcher_task
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None and stderr_task is not initiating_task:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
        transport = self._transport
        self._transport = None
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()
        self._fail_pending_futures(reset_epoch, AppServerError("app-server connection reset"))
        self._protocol_initialized = False
        self.initialized = False
        self._initialize_result = None
        self.connection_mode = "disconnected"
        await self._supervisor.stop()
        if notify_handlers and reset_epoch > 0:
            for handler in list(self._connection_reset_handlers):
                try:
                    result = handler(reset_epoch)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    emit_event(
                        component="appserver.client",
                        event="appserver.connection_reset_handler.failed",
                        level="ERROR",
                        message=str(exc) or "Connection reset handler failed",
                        data={"connection_epoch": reset_epoch, "error_type": type(exc).__name__},
                    )
        if self._transport is None:
            self.connection_mode = "disconnected"
            emit_event(
                component="appserver.client",
                event="appserver.connection.closed",
                message="App-server connection closed",
            )
            if should_reconnect:
                mark_appserver_health(
                    connected=False,
                    mode=reset_mode,
                    status="reconnecting",
                    retry_attempt=1,
                    retry_delay_s=0.0,
                )
            else:
                mark_appserver_health(
                    connected=False,
                    mode="disconnected",
                    status="disconnected",
                    retry_attempt=None,
                    retry_delay_s=None,
                    error_type=None,
                )
        if should_reconnect and self._transport is None and not self._closing:
            self._schedule_background_reconnect()

    def _schedule_background_reconnect(self) -> None:
        task = self._reconnect_task
        if self._closing or (task is not None and not task.done()):
            return
        mark_appserver_health(
            connected=False,
            mode=self.last_connection_mode,
            status="reconnecting",
            retry_attempt=1,
            retry_delay_s=0.0,
        )
        self._reconnect_task = asyncio.create_task(self._background_reconnect_loop())

    async def _background_reconnect_loop(self) -> None:
        current_task = asyncio.current_task()
        attempt = 1
        try:
            while not self._closing:
                if attempt > 1:
                    failed_attempt = min(attempt - 1, 63)
                    delay_s = self._reconnect_retry_policy.delay_after_failure(
                        failed_attempt,
                        random_float=self._random_float,
                        downward_jitter=True,
                    )
                    emit_event(
                        component="appserver.client",
                        event="appserver.reconnect.scheduled",
                        level="WARNING",
                        message="Retrying persistent app-server connection",
                        data={"attempt": attempt, "delay_s": round(delay_s, 3)},
                    )
                    mark_appserver_health(
                        connected=False,
                        mode=self.last_connection_mode,
                        status="reconnecting",
                        retry_attempt=attempt,
                        retry_delay_s=round(delay_s, 3),
                    )
                    await self._sleep_for_retry(delay_s)
                if self._closing:
                    return
                try:
                    await self._ensure_ready()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    emit_event(
                        component="appserver.client",
                        event="appserver.reconnect.failed",
                        level="WARNING",
                        message=str(exc) or "Persistent app-server reconnect failed",
                        data={"attempt": attempt, "error_type": type(exc).__name__},
                    )
                    mark_appserver_health(
                        connected=False,
                        mode=self.last_connection_mode,
                        status="reconnecting",
                        retry_attempt=attempt,
                        error_type=type(exc).__name__,
                    )
                    if self._transport is not None:
                        await self._reset_connection()
                    attempt += 1
                    continue
                emit_event(
                    component="appserver.client",
                    event="appserver.reconnect.succeeded",
                    message="Persistent app-server connection restored",
                    data={"attempt": attempt, "connection_epoch": self.connection_epoch},
                )
                mark_appserver_health(
                    connected=True,
                    mode=self.connection_mode,
                    status="connected",
                    retry_attempt=None,
                    retry_delay_s=None,
                    error_type=None,
                    health_ok=None,
                    health_status_code=None,
                    health_error_type=None,
                )
                return
        finally:
            if self._reconnect_task is current_task:
                self._reconnect_task = None

    async def _wait_for_connection_reset(self) -> None:
        current_task = asyncio.current_task()
        while self._resetting and self._reset_owner_task is not current_task:
            await asyncio.sleep(0)

    def _normalize_thread_params(self, payload: JsonDict) -> JsonDict:
        mappings = {
            "thread_id": "threadId",
            "approval_policy": "approvalPolicy",
            "sandbox_policy": "sandboxPolicy",
            "approvals_reviewer": "approvalsReviewer",
            "service_name": "serviceName",
        }
        return {mappings.get(key, key): value for key, value in payload.items()}

    def _initialize_capabilities(self) -> JsonDict:
        capabilities: JsonDict = {"optOutNotificationMethods": list(DEFAULT_OPT_OUT_NOTIFICATION_METHODS)}
        if self._experimental_api_enabled:
            capabilities["experimentalApi"] = True
        return capabilities

    def _normalize_result(self, method: str, result: JsonDict) -> JsonDict:
        if method in _TRIMMED_THREAD_METHODS:
            self._trim_thread_history(result)
        return result

    async def _drain_process_stderr(self, process: Any) -> None:
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                if isinstance(line, bytes):
                    text = line.decode("utf-8", errors="replace").rstrip()
                else:
                    text = str(line).rstrip()
                if not text:
                    continue
                emit_event(
                    component="appserver.stderr",
                    event="appserver.stderr.line",
                    level="WARNING",
                    message="App-server stderr output",
                    data=summarize_text(text),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit_event(
                component="appserver.stderr",
                event="appserver.stderr.read_failed",
                level="WARNING",
                message="Failed while draining app-server stderr",
                data={"error_type": type(exc).__name__},
            )

    def _trace_protocol_message(self, *, stage: str, payload: JsonDict) -> None:
        emit_event(
            component="appserver.protocol",
            event=f"appserver.protocol.{stage}",
            message=f"App-server protocol message {stage}",
            connection_mode=self.connection_mode,
            connection_epoch=self.connection_epoch,
            data=summarize_transport_message(payload),
        )

    def _trim_thread_history(self, result: JsonDict) -> None:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            return
        turns = thread.get("turns")
        if not isinstance(turns, list) or len(turns) <= _MAX_RECENT_THREAD_TURNS:
            return
        thread["turns"] = turns[-_MAX_RECENT_THREAD_TURNS:]

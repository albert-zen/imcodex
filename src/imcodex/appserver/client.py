from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any


JsonDict = dict[str, Any]
NotificationHandler = Callable[[JsonDict], Awaitable[None] | None]
ServerRequestHandler = Callable[[JsonDict], Awaitable[None] | None]


class AppServerError(RuntimeError):
    pass


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
        self._process: Any = None
        self._stdin: Any = None
        self._stdout: Any = None
        self._listener_task: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._pending_futures: dict[int, asyncio.Future[JsonDict]] = {}
        self._pending_server_requests: dict[str, JsonDict] = {}
        self._notification_handlers: list[NotificationHandler] = []
        self._server_request_handlers: list[ServerRequestHandler] = []
        self.initialized = False

    async def connect(self) -> None:
        await self._ensure_connected()

    async def close(self) -> None:
        await self._reset_connection()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def add_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handlers.append(handler)

    async def initialize(self) -> JsonDict:
        await self._ensure_connected()
        result = await self._request_without_initialize("initialize", {"clientInfo": self._client_info})
        await self._notify("initialized", {})
        self.initialized = True
        return result

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

    async def read_thread(self, thread_id: str) -> JsonDict:
        return await self._request("thread/read", {"threadId": thread_id})

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

    async def reply_to_server_request(self, request_id: str, result: JsonDict) -> JsonDict:
        await self._ensure_connected()
        pending = self._pending_server_requests.get(request_id)
        if pending is None:
            raise AppServerError(f"unknown pending request: {request_id}")
        for key in self._pending_request_keys(pending):
            self._pending_server_requests.pop(key, None)
        payload = {"id": pending["id"], "result": result}
        await self._send_json(payload)
        return payload

    async def _ensure_connected(self) -> None:
        if self._listener_task is not None and self._listener_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
            self._listener_task = None
            await self._reset_connection()
        if self._process is not None and getattr(self._process, "returncode", None) is not None:
            await self._reset_connection()
        if self._process is None:
            self._process = await self._supervisor.start()
            self._stdin = getattr(self._process, "stdin", None)
            self._stdout = getattr(self._process, "stdout", None)
            if self._stdin is None or self._stdout is None:
                raise AppServerError("stdio app-server process missing stdin/stdout pipes")
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
        return response["result"]

    async def _notify(self, method: str, params: JsonDict) -> None:
        await self._send_json({"method": method, "params": params})

    async def _send_json(self, payload: JsonDict) -> None:
        if self._stdin is None:
            raise AppServerError("transport is not connected")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self._stdin.write(data)
        drain = getattr(self._stdin, "drain", None)
        if callable(drain):
            result = drain()
            if inspect.isawaitable(result):
                await result

    async def _receive_loop(self) -> None:
        error: Exception | None = None
        try:
            while self._stdout is not None:
                raw = await self._stdout.readline()
                if not raw:
                    error = AppServerError("app-server connection closed")
                    break
                message = json.loads(raw.decode("utf-8"))
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
            self._process = None
            self._stdin = None
            self._stdout = None

    async def _dispatch(self, message: JsonDict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending_futures.get(int(message["id"]))
            if future is not None and not future.done():
                future.set_result(message)
            return
        if "id" in message and "method" in message:
            request_id = str(message["id"])
            for key in self._pending_request_keys(message):
                self._pending_server_requests[key] = message
            enriched = {
                "id": message["id"],
                "method": message["method"],
                "params": {
                    **(message.get("params") or {}),
                    "_request_id": request_id,
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

    async def _reset_connection(self) -> None:
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None and listener_task is not asyncio.current_task():
            if not listener_task.done():
                listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener_task
        if self._stdin is not None:
            close = getattr(self._stdin, "close", None)
            if callable(close):
                close()
        self._process = None
        self._stdin = None
        self._stdout = None
        self._pending_server_requests.clear()
        self.initialized = False
        await self._supervisor.stop()

    def _normalize_thread_params(self, payload: JsonDict) -> JsonDict:
        mappings = {
            "thread_id": "threadId",
            "approval_policy": "approvalPolicy",
            "sandbox_policy": "sandboxPolicy",
            "approvals_reviewer": "approvalsReviewer",
            "service_name": "serviceName",
        }
        return {mappings.get(key, key): value for key, value in payload.items()}

    def _pending_request_keys(self, message: JsonDict) -> set[str]:
        keys = {str(message.get("id") or "")}
        params = message.get("params")
        if isinstance(params, dict):
            native_request_id = params.get("requestId")
            if native_request_id is not None:
                keys.add(str(native_request_id))
        keys.discard("")
        return keys

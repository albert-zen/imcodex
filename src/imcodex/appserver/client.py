from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any


JsonDict = dict[str, Any]
WebSocketFactory = Callable[[str], Any]
NotificationHandler = Callable[[JsonDict], Awaitable[None] | None]
ServerRequestHandler = Callable[[JsonDict], Awaitable[None] | None]


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(
        self,
        *,
        websocket_factory: WebSocketFactory,
        transport_url: str,
        client_info: dict[str, str],
        request_timeout_s: float = 15.0,
    ) -> None:
        self._websocket_factory = websocket_factory
        self._transport_url = transport_url
        self._client_info = client_info
        self._ws: Any = None
        self._listener_task: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._pending_futures: dict[int, asyncio.Future[JsonDict]] = {}
        self._pending_requests: dict[str, JsonDict] = {}
        self._notification_handlers: list[NotificationHandler] = []
        self._server_request_handlers: list[ServerRequestHandler] = []
        self._agent_message_buffers: dict[tuple[str, str, str], list[str]] = {}
        self._request_timeout_s = request_timeout_s
        self.initialized = False
        self.last_agent_message = ""

    async def connect(self) -> None:
        if self._ws is not None:
            return
        ws = self._websocket_factory(self._transport_url)
        if isinstance(ws, Awaitable):
            ws = await ws
        self._ws = ws
        self._listener_task = asyncio.create_task(self._receive_loop())

    async def close(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        await self._reset_connection()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def add_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handlers.append(handler)

    def pending_requests(self) -> list[JsonDict]:
        return [
            {"ticket_id": ticket_id, **request}
            for ticket_id, request in self._pending_requests.items()
        ]

    async def initialize(self) -> JsonDict:
        await self._ensure_connected()
        result = await self._request(
            "initialize",
            {"clientInfo": self._client_info},
        )
        await self._notify("initialized", {})
        self.initialized = True
        return result

    async def start_thread(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        await self._ensure_ready()
        payload = dict(params or {})
        payload.update(kwargs)
        result = await self._request("thread/start", self._normalize_thread_start(payload))
        await asyncio.sleep(0)
        return result

    async def resume_thread(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        await self._ensure_ready()
        payload = dict(params or {})
        payload.update(kwargs)
        result = await self._request("thread/resume", self._normalize_thread_start(payload))
        await asyncio.sleep(0)
        return result

    async def list_threads(self, params: JsonDict | None = None, **kwargs: Any) -> JsonDict:
        await self._ensure_ready()
        payload = dict(params or {})
        payload.update(kwargs)
        result = await self._request("thread/list", payload)
        await asyncio.sleep(0)
        return result

    async def read_thread(self, thread_id: str) -> JsonDict:
        await self._ensure_ready()
        result = await self._request("thread/read", {"threadId": thread_id})
        await asyncio.sleep(0)
        return result

    async def start_turn(
        self,
        thread_id: str | None = None,
        text: str | None = None,
        **kwargs: Any,
    ) -> JsonDict:
        await self._ensure_ready()
        if thread_id is not None:
            kwargs["thread_id"] = thread_id
        if text is not None:
            kwargs["text"] = text
        result = await self._request(
            "turn/start",
            self._normalize_turn_start(kwargs),
        )
        await asyncio.sleep(0)
        return result

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> JsonDict:
        await self._ensure_ready()
        return await self._request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def steer_turn(self, thread_id: str, turn_id: str, text: str) -> JsonDict:
        await self._ensure_ready()
        return await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def reply_to_server_request(
        self,
        ticket_id: str,
        decision_or_answers: JsonDict,
    ) -> JsonDict:
        request = self._pending_requests.pop(ticket_id, None)
        if request is None:
            raise AppServerError(f"unknown pending request: {ticket_id}")
        payload = {
            "id": request["id"],
            "result": decision_or_answers,
        }
        if self._ws is not None:
            await self._send_json(payload)
        return payload

    async def _ensure_connected(self) -> None:
        if self._listener_task is not None and not self._pending_futures and self.initialized:
            await asyncio.sleep(0)
        transport_closed = self._ws is not None and getattr(self._ws, "connected", True) is False
        if transport_closed:
            await self._reset_connection()
        if self._listener_task is not None and self._listener_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
            self._listener_task = None
            await self._reset_connection()
        if self._ws is None:
            await self.connect()

    async def _ensure_ready(self) -> None:
        await self._ensure_connected()
        if not self.initialized:
            await self.initialize()

    async def _request(self, method: str, params: JsonDict) -> JsonDict:
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._pending_futures[request_id] = future
        await self._send_json({"id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=self._request_timeout_s)
        except asyncio.TimeoutError as exc:
            await self._reset_connection()
            raise AppServerError(
                f"{method} timed out after {self._request_timeout_s:.1f}s"
            ) from exc
        except Exception as exc:
            await self._reset_connection()
            raise AppServerError(str(exc) or f"{method} failed") from exc
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
        if self._ws is None:
            raise AppServerError("transport is not connected")
        await self._call_transport(self._ws.send, json.dumps(payload))

    async def _receive_loop(self) -> None:
        try:
            while self._ws is not None:
                raw = await self._call_transport(self._ws.recv)
                message = json.loads(raw)
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._ws = None
            self.initialized = False
            for future in self._pending_futures.values():
                if not future.done():
                    future.set_exception(exc)

    async def _reset_connection(self) -> None:
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None and listener_task is not asyncio.current_task():
            if not listener_task.done():
                listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener_task
        if self._ws is not None:
            close = getattr(self._ws, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await self._call_transport(close)
        self._ws = None
        self.initialized = False

    async def _dispatch(self, message: JsonDict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            future = self._pending_futures.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if "id" in message and "method" in message:
            await self._capture_server_request(message)
            return
        if "method" in message:
            if message["method"] == "item/agentMessage/delta":
                self._record_agent_delta(message)
            if message["method"] == "item/completed":
                self._record_completed_item(message)
            notification = {"method": message["method"], "params": message.get("params", {})}
            for handler in list(self._notification_handlers):
                result = handler(notification)
                if isinstance(result, Awaitable):
                    await result

    async def _call_transport(self, func, *args):
        if inspect.iscoroutinefunction(func):
            return await func(*args)
        return await asyncio.to_thread(func, *args)

    async def _capture_server_request(self, message: JsonDict) -> None:
        ticket_id = str(message["id"])
        self._pending_requests[ticket_id] = {
            "id": message["id"],
            "method": message["method"],
            "params": message.get("params", {}),
        }
        enriched = {
            "id": message["id"],
            "method": message["method"],
            "params": {
                **(message.get("params", {}) or {}),
                "_request_id": str(message["id"]),
            },
        }
        for handler in list(self._server_request_handlers):
            result = handler(enriched)
            if isinstance(result, Awaitable):
                await result

    def _record_agent_delta(self, message: JsonDict) -> None:
        params = message.get("params", {})
        key = (
            str(params.get("threadId", "")),
            str(params.get("turnId", "")),
            str(params.get("itemId", "")),
        )
        self._agent_message_buffers.setdefault(key, []).append(str(params.get("delta", "")))

    def _record_completed_item(self, message: JsonDict) -> None:
        params = message.get("params", {})
        item = params.get("item") or {}
        if item.get("type") == "agentMessage":
            text = item.get("text", "")
            key = (
                str(params.get("threadId", "")),
                str(params.get("turnId", "")),
                str(item.get("id", "")),
            )
            delta_text = "".join(self._agent_message_buffers.pop(key, []))
            self.last_agent_message = text or delta_text

    def _normalize_thread_start(self, payload: JsonDict) -> JsonDict:
        mappings = {
            "thread_id": "threadId",
            "approval_policy": "approvalPolicy",
            "service_name": "serviceName",
        }
        return {mappings.get(key, key): value for key, value in payload.items()}

    def _normalize_turn_start(self, payload: JsonDict) -> JsonDict:
        out: JsonDict = {
            "threadId": payload["thread_id"],
            "input": [{"type": "text", "text": payload["text"]}],
        }
        mappings = {
            "approval_policy": "approvalPolicy",
            "sandbox_policy": "sandboxPolicy",
        }
        for key in ("cwd", "model", "approval_policy", "sandbox_policy", "effort", "summary"):
            value = payload.get(key)
            if value is not None:
                out[mappings.get(key, key)] = value
        return out

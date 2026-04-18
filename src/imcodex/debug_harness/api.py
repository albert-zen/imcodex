from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..models import NativeThreadSnapshot


def install_debug_routes(app: FastAPI, runtime) -> None:
    @app.get("/api/debug/runtime")
    async def debug_runtime() -> dict[str, Any]:
        client = getattr(getattr(runtime, "service", None), "backend", None)
        client = getattr(client, "client", None)
        observability = getattr(runtime, "observability", None)
        health = _read_json(getattr(getattr(observability, "paths", None), "current_health_path", None))
        return {
            "instance_id": getattr(getattr(observability, "context", None), "instance_id", None),
            "health": health,
            "appserver": {
                "connection_mode": getattr(client, "connection_mode", "unknown"),
                "initialized": bool(getattr(client, "initialized", False)),
                "pending_server_request_ids": sorted(
                    str(key) for key in getattr(client, "_pending_server_requests", {}).keys()
                ),
            },
            "managed_channels": [getattr(channel, "channel_id", "unknown") for channel in getattr(runtime, "managed_channels", [])],
        }

    @app.get("/api/debug/conversation/{channel_id}/{conversation_id}")
    async def debug_conversation(channel_id: str, conversation_id: str) -> dict[str, Any]:
        store = runtime.service.store
        binding = store.get_binding(channel_id, conversation_id)
        snapshot = store.get_thread_snapshot(binding.thread_id) if binding.thread_id else None
        active = store.get_active_turn(binding.thread_id) if binding.thread_id else None
        return {
            "binding": {
                "channel_id": binding.channel_id,
                "conversation_id": binding.conversation_id,
                "thread_id": binding.thread_id,
                "bootstrap_cwd": binding.bootstrap_cwd,
            },
            "current_cwd": store.current_cwd(channel_id, conversation_id),
            "thread_snapshot": _snapshot_to_dict(snapshot),
            "active_turn": _active_turn_to_dict(active),
            "pending_requests": [_route_to_dict(route) for route in store.list_pending_requests(channel_id, conversation_id)],
        }

    @app.get("/api/debug/thread/{thread_id}")
    async def debug_thread(thread_id: str) -> dict[str, Any]:
        store = runtime.service.store
        snapshot = store.get_thread_snapshot(thread_id)
        binding = store.find_binding_by_thread_id(thread_id)
        active = store.get_active_turn(thread_id)
        return {
            "thread_snapshot": _snapshot_to_dict(snapshot),
            "binding": None
            if binding is None
            else {
                "channel_id": binding.channel_id,
                "conversation_id": binding.conversation_id,
                "thread_id": binding.thread_id,
                "bootstrap_cwd": binding.bootstrap_cwd,
            },
            "active_turn": _active_turn_to_dict(active),
        }

    @app.post("/api/debug/inject/binding")
    async def inject_binding(payload: dict[str, Any]) -> dict[str, Any]:
        store = runtime.service.store
        binding = store.bind_thread_with_cwd(
            str(payload.get("channel_id") or ""),
            str(payload.get("conversation_id") or ""),
            str(payload.get("thread_id") or ""),
            str(payload.get("cwd") or "") or None,
        )
        thread_id = binding.thread_id or ""
        if thread_id:
            store.note_thread_snapshot(
                NativeThreadSnapshot(
                    thread_id=thread_id,
                    cwd=str(payload.get("cwd") or ""),
                    preview=str(payload.get("preview") or ""),
                    status=str(payload.get("status") or "idle"),
                    name=str(payload["name"]) if payload.get("name") is not None else None,
                    path=str(payload["path"]) if payload.get("path") is not None else None,
                    source=str(payload["source"]) if payload.get("source") is not None else None,
                )
            )
        return {"ok": True}

    @app.post("/api/debug/inject/active-turn")
    async def inject_active_turn(payload: dict[str, Any]) -> dict[str, Any]:
        runtime.service.store.note_active_turn(
            str(payload.get("thread_id") or ""),
            str(payload.get("turn_id") or ""),
            str(payload.get("status") or "inProgress"),
        )
        return {"ok": True}

    @app.post("/api/debug/inject/pending-request")
    async def inject_pending_request(payload: dict[str, Any]) -> dict[str, Any]:
        route = runtime.service.store.upsert_pending_request(
            request_id=str(payload.get("request_id") or ""),
            request_handle=str(payload.get("request_handle") or "") or None,
            channel_id=str(payload.get("channel_id") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            thread_id=str(payload.get("thread_id") or "") or None,
            turn_id=str(payload.get("turn_id") or "") or None,
            kind=str(payload.get("kind") or "approval"),
            request_method=str(payload.get("request_method") or "") or None,
            transport_request_id=payload.get("transport_request_id"),
            connection_epoch=int(payload.get("connection_epoch") or 0),
            payload=dict(payload.get("payload") or {}),
        )
        return {"ok": True, "request_id": route.request_id}

    @app.post("/api/debug/inject/server-request")
    async def inject_server_request(payload: dict[str, Any]) -> dict[str, Any]:
        client = getattr(getattr(runtime, "service", None), "backend", None)
        client = getattr(client, "client", None)
        request = {
            "id": payload.get("id"),
            "method": str(payload.get("method") or ""),
            "params": {
                **dict(payload.get("payload") or {}),
                "requestId": str(payload.get("request_id") or ""),
                "threadId": str(payload.get("thread_id") or ""),
                "turnId": str(payload.get("turn_id") or ""),
                "_transport_request_id": payload.get("id"),
                "_connection_epoch": int(getattr(client, "connection_epoch", 0) or 0),
            },
        }
        await runtime.service.handle_server_request(request)
        return {"ok": True, "request_id": request["params"]["requestId"]}

    @app.post("/api/debug/inject/client-pending-request")
    async def inject_client_pending_request(payload: dict[str, Any]) -> dict[str, Any]:
        client = getattr(getattr(runtime, "service", None), "backend", None)
        client = getattr(client, "client", None)
        request_id = str(payload.get("request_id") or "")
        jsonrpc_id = payload.get("jsonrpc_id")
        if client is None or not request_id or jsonrpc_id is None:
            return {"ok": False}
        pending = getattr(client, "_pending_server_requests", None)
        if not isinstance(pending, dict):
            return {"ok": False}
        pending[request_id] = {"id": jsonrpc_id}
        return {"ok": True, "request_id": request_id}

    @app.post("/api/debug/force/client-reset")
    async def force_client_reset() -> dict[str, Any]:
        client = getattr(getattr(runtime, "service", None), "backend", None)
        client = getattr(client, "client", None)
        reset = getattr(client, "_reset_connection", None)
        if not callable(reset):
            return {"ok": False}
        await reset()
        return {"ok": True}


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _snapshot_to_dict(snapshot) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "thread_id": snapshot.thread_id,
        "cwd": snapshot.cwd,
        "preview": snapshot.preview,
        "status": snapshot.status,
        "name": snapshot.name,
        "path": snapshot.path,
        "source": snapshot.source,
    }


def _route_to_dict(route) -> dict[str, Any]:
    return {
        "request_id": route.request_id,
        "request_handle": route.request_handle,
        "channel_id": route.channel_id,
        "conversation_id": route.conversation_id,
        "thread_id": route.thread_id,
        "turn_id": route.turn_id,
        "kind": route.kind,
        "request_method": route.request_method,
        "transport_request_id": route.transport_request_id,
        "connection_epoch": route.connection_epoch,
        "payload": route.payload,
    }


def _active_turn_to_dict(active: tuple[str, str] | None) -> dict[str, Any] | None:
    if active is None:
        return None
    return {"turn_id": active[0], "status": active[1]}

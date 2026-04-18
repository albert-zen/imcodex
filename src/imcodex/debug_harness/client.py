from __future__ import annotations

import uuid
from typing import Any

import httpx

from .models import DebugRunManifest


class DebugHarnessClient:
    def __init__(self, *, http_client: Any | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=30.0)

    def send(
        self,
        *,
        manifest: DebugRunManifest,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        text: str,
        message_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        if thread_id:
            self._post_message(
                manifest=manifest,
                channel_id=channel_id,
                conversation_id=conversation_id,
                user_id=user_id,
                message_id=f"{message_id or self._message_id()}-attach",
                text=f"/thread attach {thread_id}",
            )
        return self._post_message(
            manifest=manifest,
            channel_id=channel_id,
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message_id or self._message_id(),
            text=text,
        )

    def inject_binding(
        self,
        *,
        manifest: DebugRunManifest,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        cwd: str,
        preview: str = "",
        status: str = "idle",
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/inject/binding",
            {
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "thread_id": thread_id,
                "cwd": cwd,
                "preview": preview,
                "status": status,
            },
        )

    def inject_active_turn(
        self,
        *,
        manifest: DebugRunManifest,
        thread_id: str,
        turn_id: str,
        status: str = "inProgress",
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/inject/active-turn",
            {"thread_id": thread_id, "turn_id": turn_id, "status": status},
        )

    def inject_pending_request(
        self,
        *,
        manifest: DebugRunManifest,
        request_id: str,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        turn_id: str,
        kind: str,
        request_method: str,
        payload: dict,
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/inject/pending-request",
            {
                "request_id": request_id,
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "kind": kind,
                "request_method": request_method,
                "payload": payload,
            },
        )

    def inject_client_pending_request(
        self,
        *,
        manifest: DebugRunManifest,
        request_id: str,
        jsonrpc_id: int,
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/inject/client-pending-request",
            {"request_id": request_id, "jsonrpc_id": jsonrpc_id},
        )

    def inject_server_request(
        self,
        *,
        manifest: DebugRunManifest,
        jsonrpc_id: int,
        method: str,
        request_id: str,
        thread_id: str,
        turn_id: str,
        payload: dict,
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/inject/server-request",
            {
                "id": jsonrpc_id,
                "method": method,
                "request_id": request_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "payload": payload,
            },
        )

    def force_client_reset(
        self,
        *,
        manifest: DebugRunManifest,
    ) -> dict:
        return self._post_debug(
            manifest,
            "/api/debug/force/client-reset",
            {},
        )

    def _post_message(
        self,
        *,
        manifest: DebugRunManifest,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str,
        text: str,
    ) -> dict:
        response = self.http_client.post(
            f"http://127.0.0.1:{manifest.port}/api/channels/webhook/inbound",
            json={
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "message_id": message_id,
                "text": text,
            },
        )
        response.raise_for_status()
        return response.json()

    def _post_debug(self, manifest: DebugRunManifest, path: str, payload: dict) -> dict:
        response = self.http_client.post(
            f"http://127.0.0.1:{manifest.port}{path}",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def _message_id(self) -> str:
        return f"dbg-{uuid.uuid4().hex[:12]}"

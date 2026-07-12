from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import logging
import math
from weakref import WeakValueDictionary

from ..models import InboundMessage, OutboundMessage
from ..observability.message_trace import ensure_trace_id, text_preview, text_sha256
from ..observability.runtime import emit_event
from .base import ChannelRouteContext


logger = logging.getLogger(__name__)
GENERIC_USER_ERROR_TEXT = "Request failed while talking to Codex. Please try again."
EXPIRED_REPLAY_TEXT = (
    "This message was already processed, but its cached reply has expired. "
    "Send a new message if you still need the result."
)


class UnifiedChannelMiddleware:
    def __init__(self, *, service) -> None:
        self.service = service
        self._conversation_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
        self._conversation_locks_guard = asyncio.Lock()

    async def handle_inbound(
        self,
        adapter,
        inbound: InboundMessage,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        trace_id = ensure_trace_id(inbound)
        content_sha = text_sha256(inbound.text)
        emit_event(
            component=f"channels.{adapter.channel_id}",
            event="message.inbound.received",
            message="Inbound channel message received",
            trace_id=trace_id,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            user_id=inbound.user_id,
            message_id=inbound.message_id,
            data={
                "reply_to_message_id": reply_to_message_id,
                "text_length": len(inbound.text),
                "text_preview": text_preview(inbound.text),
                "text_sha256": content_sha,
            },
        )
        conversation_lock = await self._get_conversation_lock(
            inbound.channel_id,
            inbound.conversation_id,
        )
        async with conversation_lock:
            await self._handle_serialized(
                adapter=adapter,
                inbound=inbound,
                reply_to_message_id=reply_to_message_id,
                trace_id=trace_id,
                content_sha=content_sha,
            )

    async def _handle_serialized(
        self,
        *,
        adapter,
        inbound: InboundMessage,
        reply_to_message_id: str | None,
        trace_id: str,
        content_sha: str,
    ) -> None:
        if self._should_drop_duplicate_inbound(inbound=inbound, text_fingerprint=content_sha):
            await self._ensure_inbound_durable(inbound)
            emit_event(
                component=f"channels.{adapter.channel_id}",
                event="message.inbound.duplicate_dropped",
                message="Dropped duplicate inbound message",
                trace_id=trace_id,
                channel_id=inbound.channel_id,
                conversation_id=inbound.conversation_id,
                user_id=inbound.user_id,
                message_id=inbound.message_id,
                data={
                    "reply_to_message_id": reply_to_message_id,
                    "text_length": len(inbound.text),
                    "text_preview": text_preview(inbound.text),
                    "text_sha256": content_sha,
                    "stable_message_id": bool(inbound.message_id),
                },
            )
            replay = self._processed_inbound_response(inbound)
            if replay is None and inbound.message_id:
                metadata: dict[str, object] = {
                    "trace_id": trace_id,
                    "delivery_id": self._delivery_id(inbound, 0, namespace="expired-response"),
                    "cached_response_expired": True,
                }
                if reply_to_message_id:
                    metadata["reply_to_message_id"] = reply_to_message_id
                replay = [
                    OutboundMessage(
                        channel_id=adapter.channel_id,
                        conversation_id=inbound.conversation_id,
                        message_type="error",
                        text=EXPIRED_REPLAY_TEXT,
                        metadata=metadata,
                    )
                ]
            if replay is None:
                replay = []
            await self._deliver_messages(adapter=adapter, messages=replay)
            after_commit = getattr(adapter, "after_inbound_committed", None)
            if callable(after_commit):
                await after_commit()
            return
        self._note_inbound_message(inbound)
        try:
            outbound = await self.service.handle_inbound(inbound)
        except Exception:
            logger.exception("%s inbound handling failed", adapter.channel_id)
            metadata = {}
            if reply_to_message_id:
                metadata["reply_to_message_id"] = reply_to_message_id
            metadata["trace_id"] = trace_id
            error_message = OutboundMessage(
                channel_id=adapter.channel_id,
                conversation_id=inbound.conversation_id,
                message_type="error",
                text=GENERIC_USER_ERROR_TEXT,
                metadata=metadata,
            )
            outbound = [error_message]
        prepared: list[OutboundMessage] = []
        for message in outbound:
            if message.channel_id != adapter.channel_id:
                continue
            message.metadata = self._json_safe_mapping(message.metadata)
            message.metadata.setdefault("trace_id", trace_id)
            if reply_to_message_id:
                message.metadata.setdefault("reply_to_message_id", reply_to_message_id)
            prepared.append(message)
        for index, message in enumerate(prepared):
            if inbound.message_id:
                message.metadata.setdefault(
                    "delivery_id",
                    self._delivery_id(inbound, index),
                )
        await self._mark_inbound_processed(
            inbound=inbound,
            text_fingerprint=content_sha,
            response_payload=[asdict(message) for message in prepared],
        )
        await self._deliver_messages(adapter=adapter, messages=prepared)
        after_commit = getattr(adapter, "after_inbound_committed", None)
        if callable(after_commit):
            await after_commit()

    async def _get_conversation_lock(
        self,
        channel_id: str,
        conversation_id: str,
    ) -> asyncio.Lock:
        key = (channel_id, conversation_id)
        async with self._conversation_locks_guard:
            lock = self._conversation_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._conversation_locks[key] = lock
            return lock

    def get_route_context(self, channel_id: str, conversation_id: str) -> ChannelRouteContext:
        store = getattr(self.service, "store", None)
        get_binding = getattr(store, "get_binding", None)
        if not callable(get_binding):
            return ChannelRouteContext()
        binding = get_binding(channel_id, conversation_id)
        reply_context = getattr(binding, "reply_context", None)
        if not isinstance(reply_context, dict):
            return ChannelRouteContext()
        return ChannelRouteContext(
            admitted_user_id=str(reply_context.get("last_inbound_user_id") or ""),
            last_inbound_message_id=str(reply_context.get("last_inbound_message_id") or ""),
        )

    def _note_inbound_message(self, inbound: InboundMessage) -> None:
        store = getattr(self.service, "store", None)
        if store is None:
            return
        note = getattr(store, "note_inbound_message", None)
        if callable(note):
            note(
                inbound.channel_id,
                inbound.conversation_id,
                inbound.message_id,
                user_id=inbound.user_id,
            )

    async def _mark_inbound_processed(
        self,
        *,
        inbound: InboundMessage,
        text_fingerprint: str,
        response_payload: list[dict],
    ) -> None:
        store = getattr(self.service, "store", None)
        if store is None:
            return
        commit = getattr(store, "commit_inbound_message_processed", None)
        if callable(commit):
            await commit(
                channel_id=inbound.channel_id,
                conversation_id=inbound.conversation_id,
                user_id=inbound.user_id,
                message_id=inbound.message_id,
                text_fingerprint=text_fingerprint,
                response_payload=response_payload,
            )
            return
        mark = getattr(store, "mark_inbound_message_processed", None)
        if callable(mark):
            mark(
                channel_id=inbound.channel_id,
                conversation_id=inbound.conversation_id,
                user_id=inbound.user_id,
                message_id=inbound.message_id,
                text_fingerprint=text_fingerprint,
                response_payload=response_payload,
            )

    async def _ensure_inbound_durable(self, inbound: InboundMessage) -> None:
        store = getattr(self.service, "store", None)
        ensure = getattr(store, "ensure_inbound_message_durable", None)
        if callable(ensure):
            await ensure(
                inbound.channel_id,
                inbound.conversation_id,
                inbound.message_id,
            )

    def _processed_inbound_response(
        self,
        inbound: InboundMessage,
    ) -> list[OutboundMessage] | None:
        store = getattr(self.service, "store", None)
        get_response = getattr(store, "get_processed_inbound_response", None)
        if not callable(get_response):
            return None
        payload = get_response(
            inbound.channel_id,
            inbound.conversation_id,
            inbound.message_id,
        )
        if not isinstance(payload, list):
            return None
        try:
            return [OutboundMessage(**item) for item in payload]
        except (TypeError, ValueError):
            logger.error("Invalid cached outbound response for %s", inbound.message_id)
            return None

    async def _deliver_messages(
        self,
        *,
        adapter,
        messages: list[OutboundMessage],
    ) -> None:
        for index, message in enumerate(messages):
            self._emit_outbound_event(
                adapter=adapter,
                message=message,
                event="message.outbound.sending",
                emitted_at="before_send",
                outbound_index=index,
            )
            await adapter.send_message(message)
            self._emit_outbound_event(
                adapter=adapter,
                message=message,
                event="message.outbound.sent",
                emitted_at="after_send",
                outbound_index=index,
            )

    @staticmethod
    def _delivery_id(
        inbound: InboundMessage,
        outbound_index: int,
        *,
        namespace: str = "response",
    ) -> str:
        digest = hashlib.sha256()
        for value in (
            namespace,
            inbound.channel_id,
            inbound.conversation_id,
            inbound.message_id,
            str(outbound_index),
        ):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return f"imcodex:{digest.hexdigest()}"

    @classmethod
    def _json_safe_mapping(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        return {str(key): cls._json_safe_value(item, depth=1) for key, item in value.items()}

    @classmethod
    def _json_safe_value(cls, value: object, *, depth: int) -> object:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if depth >= 8:
            return None
        if isinstance(value, dict):
            return {str(key): cls._json_safe_value(item, depth=depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe_value(item, depth=depth + 1) for item in value]
        return None

    def _should_drop_duplicate_inbound(self, *, inbound: InboundMessage, text_fingerprint: str) -> bool:
        store = getattr(self.service, "store", None)
        if store is None:
            return False
        deduper = getattr(store, "should_drop_duplicate_inbound_message", None)
        if not callable(deduper):
            return False
        return bool(
            deduper(
                channel_id=inbound.channel_id,
                conversation_id=inbound.conversation_id,
                user_id=inbound.user_id,
                message_id=inbound.message_id,
                text_fingerprint=text_fingerprint,
            )
        )

    def _emit_outbound_event(
        self,
        *,
        adapter,
        message: OutboundMessage,
        event: str,
        emitted_at: str,
        outbound_index: int,
    ) -> None:
        trace_id = message.metadata.get("trace_id")
        emit_event(
            component=f"channels.{adapter.channel_id}",
            event=event,
            message="Outbound channel message emitted",
            trace_id=trace_id,
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            request_id=message.request_id,
            data={
                "emitted_at": emitted_at,
                "message_type": message.message_type,
                "outbound_index": outbound_index,
                "reply_to_message_id": message.metadata.get("reply_to_message_id"),
                "text_length": len(message.text),
                "text_preview": text_preview(message.text),
                "text_sha256": text_sha256(message.text),
            },
        )

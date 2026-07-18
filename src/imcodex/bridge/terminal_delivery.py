from __future__ import annotations

import asyncio
from dataclasses import asdict

from ..models import OutboundMessage
from ..observability.message_trace import text_sha256
from ..observability.runtime import emit_event


_TERMINAL_DELIVERY_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 5.0, 10.0)


class TerminalDeliveryMixin:
    def _init_terminal_delivery(self) -> None:
        self._terminal_delivery_retry_task: asyncio.Task[None] | None = None
        self._terminal_delivery_closed = False
        self._terminal_delivery_ack_persistence_pending = False

    async def _close_terminal_delivery(self) -> None:
        self._terminal_delivery_closed = True
        retry_task = self._terminal_delivery_retry_task
        self._terminal_delivery_retry_task = None
        if retry_task is not None:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)

    async def _deliver_terminal_message(
        self,
        terminal_key: tuple[str, str],
        message: OutboundMessage,
    ) -> tuple[list[OutboundMessage], bool]:
        thread_id, turn_id = terminal_key
        prepare = getattr(self.outbound_sink, "prepare_durable_message", None)
        if callable(prepare):
            prepare(message)
        pending = self.store.stage_terminal_delivery(
            thread_id=thread_id,
            turn_id=turn_id,
            message=asdict(message),
        )
        if pending.message is not None:
            message = OutboundMessage(**pending.message)
        try:
            await self.store.flush_pending_writes()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit_event(
                component="bridge",
                event="bridge.terminal_delivery.persistence_failed",
                level="ERROR",
                message="Terminal IM delivery persistence failed and was queued for retry",
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                data={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "delivery_id": message.metadata.get("delivery_id"),
                    "error_type": type(exc).__name__,
                },
            )
            self._schedule_terminal_delivery_retry()
            return [message], False
        try:
            outbound = await self._emit_required(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit_event(
                component="bridge",
                event="bridge.terminal_delivery.failed",
                level="WARNING",
                message="Terminal IM delivery failed and was queued for retry",
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                data={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "delivery_id": message.metadata.get("delivery_id"),
                    "error_type": type(exc).__name__,
                },
            )
            self._schedule_terminal_delivery_retry()
            return [message], False
        self.store.complete_terminal_delivery(thread_id, turn_id)
        await self._flush_terminal_delivery_ack(
            thread_id=thread_id,
            turn_id=turn_id,
            message=message,
        )
        emit_event(
            component="bridge",
            event="bridge.terminal_delivery.succeeded",
            message="Terminal IM delivery completed",
            channel_id=message.channel_id,
            conversation_id=message.conversation_id,
            data={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "delivery_id": message.metadata.get("delivery_id"),
                "text_length": len(message.text),
                "text_sha256": text_sha256(message.text),
            },
        )
        return outbound, True

    def _schedule_terminal_delivery_retry(self) -> None:
        if self._terminal_delivery_closed:
            return
        task = self._terminal_delivery_retry_task
        if task is not None and not task.done():
            return
        self._terminal_delivery_retry_task = asyncio.create_task(
            self._retry_pending_terminal_deliveries(),
            name="imcodex-terminal-delivery-retry",
        )

    async def _retry_pending_terminal_deliveries(self) -> None:
        current_task = asyncio.current_task()
        attempt = 0
        try:
            while not self._terminal_delivery_closed:
                pending = [
                    item
                    for item in self.store.list_pending_terminal_deliveries()
                    if item.message is not None
                ]
                if not pending and not self._terminal_delivery_ack_persistence_pending:
                    return
                delivered = await self._deliver_pending_terminal_once()
                ack_persisted = await self._retry_terminal_delivery_ack_persistence()
                if delivered or ack_persisted:
                    attempt = 0
                    continue
                delay_s = _TERMINAL_DELIVERY_RETRY_DELAYS_S[
                    min(attempt, len(_TERMINAL_DELIVERY_RETRY_DELAYS_S) - 1)
                ]
                attempt += 1
                await asyncio.sleep(delay_s)
        finally:
            if self._terminal_delivery_retry_task is current_task:
                self._terminal_delivery_retry_task = None

    async def _deliver_pending_terminal_once(self) -> bool:
        delivered_any = False
        state_changed = False
        async with self._terminal_projection_lock:
            try:
                self.store.retry_terminal_delivery_persistence()
                await self.store.flush_pending_writes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                emit_event(
                    component="bridge",
                    event="bridge.terminal_delivery.persistence_retry_failed",
                    level="ERROR",
                    message="Persisted terminal IM delivery could not be made durable",
                    data={"error_type": type(exc).__name__},
                )
                return False
            for pending in self.store.list_pending_terminal_deliveries():
                if pending.message is None:
                    continue
                payload = dict(pending.message)
                if not payload.get("channel_id") or not payload.get("conversation_id"):
                    self.store.complete_terminal_delivery(pending.thread_id, pending.turn_id)
                    state_changed = True
                    continue
                try:
                    message = OutboundMessage(**payload)
                except (TypeError, ValueError):
                    self.store.complete_terminal_delivery(pending.thread_id, pending.turn_id)
                    state_changed = True
                    emit_event(
                        component="bridge",
                        event="bridge.terminal_delivery.invalid",
                        level="ERROR",
                        message="Discarded an invalid persisted terminal delivery",
                        data={"thread_id": pending.thread_id, "turn_id": pending.turn_id},
                    )
                    continue
                try:
                    await self._emit_required(message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    emit_event(
                        component="bridge",
                        event="bridge.terminal_delivery.retry_failed",
                        level="WARNING",
                        message="Persisted terminal IM delivery retry failed",
                        channel_id=message.channel_id,
                        conversation_id=message.conversation_id,
                        data={
                            "thread_id": pending.thread_id,
                            "turn_id": pending.turn_id,
                            "delivery_id": message.metadata.get("delivery_id"),
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                self.store.complete_terminal_delivery(pending.thread_id, pending.turn_id)
                state_changed = True
                self._remember_terminal_delivery((pending.thread_id, pending.turn_id))
                delivered_any = True
                emit_event(
                    component="bridge",
                    event="bridge.terminal_delivery.retry_succeeded",
                    message="Persisted terminal IM delivery retry completed",
                    channel_id=message.channel_id,
                    conversation_id=message.conversation_id,
                    data={
                        "thread_id": pending.thread_id,
                        "turn_id": pending.turn_id,
                        "delivery_id": message.metadata.get("delivery_id"),
                    },
                )
            if state_changed:
                try:
                    await self.store.flush_pending_writes()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._terminal_delivery_ack_persistence_pending = True
                    emit_event(
                        component="bridge",
                        event="bridge.terminal_delivery.ack_persistence_failed",
                        level="ERROR",
                        message="Terminal delivery acknowledgement persistence failed",
                        data={"error_type": type(exc).__name__},
                    )
                    self._schedule_terminal_delivery_retry()
        return delivered_any

    async def _flush_terminal_delivery_ack(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message: OutboundMessage,
    ) -> None:
        try:
            await self.store.flush_pending_writes()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._terminal_delivery_ack_persistence_pending = True
            emit_event(
                component="bridge",
                event="bridge.terminal_delivery.ack_persistence_failed",
                level="ERROR",
                message="Terminal delivery succeeded but its acknowledgement was not durable",
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                data={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "delivery_id": message.metadata.get("delivery_id"),
                    "error_type": type(exc).__name__,
                },
            )
            self._schedule_terminal_delivery_retry()

    async def _retry_terminal_delivery_ack_persistence(self) -> bool:
        if not self._terminal_delivery_ack_persistence_pending:
            return False
        try:
            self.store.retry_state_persistence()
            await self.store.flush_pending_writes()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit_event(
                component="bridge",
                event="bridge.terminal_delivery.ack_persistence_retry_failed",
                level="ERROR",
                message="Terminal delivery acknowledgement is still not durable",
                data={"error_type": type(exc).__name__},
            )
            return False
        self._terminal_delivery_ack_persistence_pending = False
        return True

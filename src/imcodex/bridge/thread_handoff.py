from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace

from ..appserver import AppServerError, ThreadSelectionError, normalize_appserver_message
from ..models import InboundMessage, OutboundMessage
from ..observability.runtime import emit_event
from .thread_history import render_thread_catchup, render_thread_history


_ACTIVE_THREAD_STATUSES = frozenset({"active", "inprogress", "in_progress", "running", "working"})
_THREAD_OUTPUT_GATE_EVENT_LIMIT = 1024
_THREAD_OUTPUT_RETRY_DELAYS_S = (0.1, 0.5, 1.0, 2.0, 5.0)


@dataclass(slots=True)
class _BufferedNativeMessage:
    kind: str
    payload: dict
    journal_sequence: int
    projected_message: OutboundMessage | None = None
    projection_prepared: bool = False


@dataclass(slots=True)
class _ThreadOutputGate:
    channel_id: str
    conversation_id: str
    thread_id: str
    inbound_message_id: str
    messages: deque[_BufferedNativeMessage] = field(default_factory=deque)
    capacity_available: asyncio.Event = field(default_factory=asyncio.Event)
    release_through_sequence: int = 0
    response_delivered: bool = False
    response_messages: tuple[OutboundMessage, ...] = ()
    drain_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.capacity_available.set()


class ThreadHandoffMixin:
    def _init_thread_handoff(self) -> None:
        self._thread_output_gate_lock = asyncio.Lock()
        self._thread_output_admission_condition = asyncio.Condition()
        self._thread_output_next_dispatch_sequence = 1
        self._thread_output_admitted_sequence = 0
        self._thread_handoff_closed = False
        self._thread_output_gates_by_thread: dict[str, _ThreadOutputGate] = {}
        self._thread_output_gates_by_route: dict[tuple[str, str], _ThreadOutputGate] = {}

    async def _close_thread_handoff(self) -> None:
        async with self._thread_output_admission_condition:
            self._thread_handoff_closed = True
            self._thread_output_admission_condition.notify_all()
        retry_tasks: list[asyncio.Task[None]] = []
        async with self._thread_output_gate_lock:
            for gate in self._thread_output_gates_by_thread.values():
                gate.capacity_available.set()
                if gate.retry_task is not None:
                    gate.retry_task.cancel()
                    retry_tasks.append(gate.retry_task)
            self._thread_output_gates_by_thread.clear()
            self._thread_output_gates_by_route.clear()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)

    async def _reset_thread_output_admission(self) -> None:
        sequence_reader = getattr(self.backend, "native_dispatch_sequence", None)
        sequence = 0
        if callable(sequence_reader):
            try:
                sequence = int(sequence_reader() or 0)
            except (TypeError, ValueError):
                sequence = 0
        async with self._thread_output_admission_condition:
            self._thread_output_admitted_sequence = max(
                self._thread_output_admitted_sequence,
                sequence,
            )
            self._thread_output_next_dispatch_sequence = sequence + 1
            self._thread_output_admission_condition.notify_all()

    async def _switch_thread(
        self,
        message: InboundMessage,
        thread_id: str,
        *,
        history_limit: object,
        catchup_limit: object,
        verb: str,
    ) -> list[OutboundMessage]:
        try:
            gate = await self._begin_thread_output_gate(
                message.channel_id,
                message.conversation_id,
                thread_id,
                message.message_id,
            )
        except AppServerError as exc:
            return [self._message(message, "status", self._safe_appserver_error(exc))]
        try:
            attached_id = await self.backend.attach_thread(
                message.channel_id,
                message.conversation_id,
                thread_id,
            )
        except ThreadSelectionError as exc:
            await self._discard_thread_output_gate(gate, note="thread switch failed")
            return [self._message(message, "error", str(exc))]
        except AppServerError as exc:
            await self._discard_thread_output_gate(gate, note="thread switch failed")
            return [
                self._message(
                    message,
                    "status",
                    f"Thread could not be attached: {self._safe_appserver_error(exc)}.",
                )
            ]
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="thread switch cancelled")
            raise
        except Exception:
            await self._discard_thread_output_gate(gate, note="thread switch failed")
            raise

        self._seal_thread_output_gate(gate)
        snapshot = self.store.get_thread_snapshot(attached_id)
        label = self._thread_label(snapshot) if snapshot is not None else attached_id
        status = snapshot.status if snapshot is not None else "idle"
        running = (
            self.store.get_active_turn(attached_id) is not None
            or self._thread_status_is_active(status)
        )
        lines = [f"{verb} {label}.", f"State: {'Working' if running else 'Idle'}"]
        if snapshot is not None and snapshot.cwd:
            lines.append(f"CWD: {snapshot.cwd}")

        requested_history = self._positive_int(history_limit)
        requested_catchup = self._positive_int(catchup_limit)
        if running:
            if requested_history is not None:
                lines.append(
                    f"History was not shown because this thread is currently running; "
                    f"ignored --history {requested_history}."
                )
            lines.append("Now following native updates for this thread here.")
            outbound = [self._message(message, "status", "\n".join(lines))]
            if requested_catchup is not None:
                outbound.extend(
                    await self._read_thread_catchup(
                        message,
                        gate,
                        limit=requested_catchup,
                    )
                )
            return outbound

        outbound = [self._message(message, "status", "\n".join(lines))]
        if requested_catchup is not None:
            outbound.extend(
                await self._read_thread_catchup(
                    message,
                    gate,
                    limit=requested_catchup,
                )
            )
            return outbound
        if requested_history is None:
            return outbound
        try:
            payload = await self.backend.read_thread_history(
                message.channel_id,
                message.conversation_id,
                limit=requested_history,
            )
        except AppServerError as exc:
            self._seal_thread_output_gate(gate)
            text = f"Thread history could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
            outbound.append(self._message(message, "command_result", text))
            return outbound
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="thread history query cancelled")
            raise
        except Exception:
            await self._discard_thread_output_gate(gate, note="thread history query failed")
            raise
        self._seal_thread_output_gate(gate)
        outbound.append(
            self._message(
                message,
                "command_result",
                render_thread_history(payload, limit=requested_history),
            )
        )
        return outbound

    async def _handle_direct_thread_pick(
        self,
        message: InboundMessage,
        *,
        query: str,
        history_limit: object,
        catchup_limit: object,
    ) -> list[OutboundMessage]:
        try:
            result = await self.backend.query_all_threads(
                message.channel_id,
                message.conversation_id,
                search_term=query,
            )
            if len(result.threads) == 1:
                return await self._switch_thread(
                    message,
                    result.threads[0].thread_id,
                    history_limit=history_limit,
                    catchup_limit=catchup_limit,
                    verb="Switched to",
                )
            if result.threads:
                text = await self._render_threads(
                    message,
                    page=1,
                    query=query,
                    refresh=False,
                    catalog=result.threads,
                )
            else:
                text = await self._render_threads(
                    message,
                    page=1,
                    query=None,
                    refresh=True,
                )
        except AppServerError:
            text = (
                "Threads could not be refreshed from Codex right now. "
                "Use /status, /thread read, or try /pick again in a moment."
            )
            return [self._message(message, "status", text)]
        return [self._message(message, "command_result", text)]

    async def _handle_thread_catchup_command(
        self,
        message: InboundMessage,
        *,
        limit: int,
    ) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.thread_id is None:
            return [self._message(message, "command_result", "No active thread.")]
        try:
            gate = await self._begin_thread_output_gate(
                message.channel_id,
                message.conversation_id,
                binding.thread_id,
                message.message_id,
            )
        except AppServerError as exc:
            return [self._message(message, "status", self._safe_appserver_error(exc))]
        self._seal_thread_output_gate(gate)
        return await self._read_thread_catchup(message, gate, limit=limit)

    async def _read_thread_catchup(
        self,
        message: InboundMessage,
        gate: _ThreadOutputGate,
        *,
        limit: int,
    ) -> list[OutboundMessage]:
        try:
            payload = await self.backend.read_thread_history(
                message.channel_id,
                message.conversation_id,
                limit=1,
                page=1,
            )
        except AppServerError as exc:
            self._seal_thread_output_gate(gate)
            text = f"Recent activity could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
            return [self._message(message, "command_result", text)]
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="thread catch-up query cancelled")
            raise
        except Exception:
            await self._discard_thread_output_gate(gate, note="thread catch-up query failed")
            raise
        self._seal_thread_output_gate(gate)
        return [
            self._message(
                message,
                "command_result",
                render_thread_catchup(payload, limit=limit),
            )
        ]

    async def _handle_thread_history_command(
        self,
        message: InboundMessage,
        *,
        limit: int,
        page: int = 1,
    ) -> list[OutboundMessage]:
        binding = self.store.get_binding(message.channel_id, message.conversation_id)
        if binding.thread_id is None:
            return [self._message(message, "command_result", "No active thread.")]
        try:
            gate = await self._begin_thread_output_gate(
                message.channel_id,
                message.conversation_id,
                binding.thread_id,
                message.message_id,
            )
        except AppServerError as exc:
            return [self._message(message, "status", self._safe_appserver_error(exc))]
        try:
            snapshot = await self.backend.read_thread(
                message.channel_id,
                message.conversation_id,
                binding.thread_id,
            )
        except AppServerError as exc:
            self._seal_thread_output_gate(gate)
            text = f"Thread history could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
            return [self._message(message, "command_result", text)]
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="thread history status query cancelled")
            raise
        except Exception:
            await self._discard_thread_output_gate(gate, note="thread history status query failed")
            raise
        self._seal_thread_output_gate(gate)
        if snapshot is None:
            return [self._message(message, "command_result", "Thread history is not available.")]
        try:
            payload = await self.backend.read_thread_history(
                message.channel_id,
                message.conversation_id,
                limit=limit,
                page=page,
            )
        except AppServerError as exc:
            self._seal_thread_output_gate(gate)
            text = f"Thread history could not be queried from Codex right now: {self._safe_appserver_error(exc)}."
            return [self._message(message, "command_result", text)]
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="thread history query cancelled")
            raise
        except Exception:
            await self._discard_thread_output_gate(gate, note="thread history query failed")
            raise
        self._seal_thread_output_gate(gate)
        return [
            self._message(
                message,
                "command_result",
                render_thread_history(payload, limit=limit),
            )
        ]

    async def _begin_thread_output_gate(
        self,
        channel_id: str,
        conversation_id: str,
        thread_id: str,
        inbound_message_id: str,
    ) -> _ThreadOutputGate:
        can_deliver = getattr(self.outbound_sink, "can_deliver", None)
        if callable(can_deliver) and not can_deliver(channel_id):
            raise AppServerError(
                "Live thread handoff requires outbound delivery for this channel. "
                "Configure the generic webhook outbound URL and retry."
            )
        route = (channel_id, conversation_id)
        async with self._thread_output_gate_lock:
            existing = self._thread_output_gates_by_route.get(route)
            if existing is not None:
                if (
                    existing.thread_id == thread_id
                    and existing.inbound_message_id == inbound_message_id
                ):
                    return existing
                raise AppServerError(
                    "A previous thread switch response is still waiting to be delivered. "
                    "Retry the original IM message before switching again."
                )
            existing_thread = self._thread_output_gates_by_thread.get(thread_id)
            if existing_thread is not None:
                raise AppServerError("this thread is already being attached to another conversation")
            gate = _ThreadOutputGate(
                channel_id=channel_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                inbound_message_id=inbound_message_id,
            )
            self._thread_output_gates_by_route[route] = gate
            self._thread_output_gates_by_thread[thread_id] = gate
            return gate

    async def after_inbound_delivery(
        self,
        inbound: InboundMessage,
        *,
        succeeded: bool,
    ) -> None:
        route = (inbound.channel_id, inbound.conversation_id)
        async with self._thread_output_gate_lock:
            gate = self._thread_output_gates_by_route.get(route)
        if gate is None:
            return
        matches_response = gate.inbound_message_id == inbound.message_id
        if not matches_response and not gate.response_delivered:
            return
        if not succeeded:
            if matches_response:
                emit_event(
                    component="bridge",
                    event="bridge.thread_output_gate.response_retry_pending",
                    level="WARNING",
                    message="Thread switch response delivery failed; preserving native output order for retry",
                    data={
                        "channel_id": gate.channel_id,
                        "conversation_id": gate.conversation_id,
                        "thread_id": gate.thread_id,
                        "inbound_message_id": gate.inbound_message_id,
                    },
                )
            return
        if matches_response:
            gate.response_delivered = True
        try:
            await self._drain_thread_output_gate(gate)
        except asyncio.CancelledError:
            await self._discard_thread_output_gate(gate, note="switch output delivery cancelled")
            raise
        except Exception as exc:
            self._schedule_thread_output_retry(gate)
            emit_event(
                component="bridge",
                event="bridge.thread_output_gate.delivery_retry_scheduled",
                level="WARNING",
                message="Buffered native output delivery failed; retry scheduled",
                data={"error_type": type(exc).__name__, "thread_id": gate.thread_id},
            )

    async def remember_inbound_response(
        self,
        inbound: InboundMessage,
        messages: list[OutboundMessage],
    ) -> None:
        route = (inbound.channel_id, inbound.conversation_id)
        async with self._thread_output_gate_lock:
            gate = self._thread_output_gates_by_route.get(route)
            if gate is None or gate.inbound_message_id != inbound.message_id:
                return
            gate.response_messages = tuple(self._clone_outbound_message(message) for message in messages)

    async def pending_inbound_response(
        self,
        inbound: InboundMessage,
    ) -> list[OutboundMessage] | None:
        route = (inbound.channel_id, inbound.conversation_id)
        async with self._thread_output_gate_lock:
            gate = self._thread_output_gates_by_route.get(route)
            if (
                gate is None
                or gate.inbound_message_id != inbound.message_id
                or not gate.response_messages
            ):
                return None
            return [self._clone_outbound_message(message) for message in gate.response_messages]

    async def _buffer_thread_output(
        self,
        *,
        kind: str,
        payload: dict,
        journal_sequence: int,
    ) -> str | None:
        dispatch_sequence = self._dispatch_sequence(payload)
        if dispatch_sequence is None:
            return await self._buffer_thread_output_unordered(
                kind=kind,
                payload=payload,
                journal_sequence=journal_sequence,
            )
        async with self._thread_output_admission_condition:
            while (
                dispatch_sequence > self._thread_output_next_dispatch_sequence
                and not self._thread_handoff_closed
            ):
                await self._thread_output_admission_condition.wait()
            if self._thread_handoff_closed:
                return None
            if dispatch_sequence < self._thread_output_next_dispatch_sequence:
                return None
            try:
                return await self._buffer_thread_output_unordered(
                    kind=kind,
                    payload=payload,
                    journal_sequence=journal_sequence,
                )
            finally:
                self._thread_output_admitted_sequence = dispatch_sequence
                self._thread_output_next_dispatch_sequence = dispatch_sequence + 1
                self._thread_output_admission_condition.notify_all()

    async def _buffer_thread_output_unordered(
        self,
        *,
        kind: str,
        payload: dict,
        journal_sequence: int,
    ) -> str | None:
        event = normalize_appserver_message(payload)
        if not event.thread_id:
            return None
        while True:
            async with self._thread_output_gate_lock:
                gate = self._thread_output_gates_by_thread.get(event.thread_id)
                if gate is None:
                    return None
                if len(gate.messages) < _THREAD_OUTPUT_GATE_EVENT_LIMIT:
                    gate.messages.append(
                        _BufferedNativeMessage(
                            kind=kind,
                            payload=payload,
                            journal_sequence=journal_sequence,
                        )
                    )
                    if len(gate.messages) >= _THREAD_OUTPUT_GATE_EVENT_LIMIT:
                        gate.capacity_available.clear()
                    return "buffered"
                gate.capacity_available.clear()
                capacity_available = gate.capacity_available
            await capacity_available.wait()

    async def _drain_thread_output_gate(self, gate: _ThreadOutputGate) -> None:
        async with gate.drain_lock:
            await self._drain_thread_output_gate_locked(gate)

    async def _drain_thread_output_gate_locked(self, gate: _ThreadOutputGate) -> None:
        while True:
            async with self._thread_output_gate_lock:
                current = self._thread_output_gates_by_thread.get(gate.thread_id)
                if current is not gate:
                    return
                if gate.messages:
                    buffered = gate.messages.popleft()
                    waiting_for_admission = False
                elif self._thread_output_admitted_sequence < gate.release_through_sequence:
                    buffered = None
                    waiting_for_admission = True
                else:
                    buffered = None
                    waiting_for_admission = False
                    self._remove_thread_output_gate_locked(gate)
            if waiting_for_admission:
                await self._wait_for_thread_output_admission(gate.release_through_sequence)
                continue
            if buffered is not None:
                try:
                    if buffered.kind == "notification":
                        def capture_projection(message: OutboundMessage | None) -> None:
                            buffered.projected_message = message
                            buffered.projection_prepared = True

                        await self._process_notification(
                            buffered.payload,
                            buffered.journal_sequence,
                            replay_message=buffered.projected_message,
                            replay_prepared=buffered.projection_prepared,
                            capture_projection=capture_projection,
                        )
                    else:
                        await self._process_server_request(
                            buffered.payload,
                            buffered.journal_sequence,
                        )
                except BaseException:
                    async with self._thread_output_gate_lock:
                        if self._thread_output_gates_by_thread.get(gate.thread_id) is gate:
                            gate.messages.appendleft(buffered)
                            gate.capacity_available.clear()
                    raise
                async with self._thread_output_gate_lock:
                    if (
                        self._thread_output_gates_by_thread.get(gate.thread_id) is gate
                        and len(gate.messages) < _THREAD_OUTPUT_GATE_EVENT_LIMIT
                    ):
                        gate.capacity_available.set()
                continue
            return

    def _schedule_thread_output_retry(self, gate: _ThreadOutputGate) -> None:
        task = gate.retry_task
        if task is not None and not task.done():
            return
        gate.retry_task = asyncio.create_task(
            self._retry_thread_output_gate(gate),
            name=f"imcodex-thread-output-retry-{gate.thread_id}",
        )

    async def _retry_thread_output_gate(self, gate: _ThreadOutputGate) -> None:
        current_task = asyncio.current_task()
        attempt = 0
        try:
            while True:
                delay_s = _THREAD_OUTPUT_RETRY_DELAYS_S[
                    min(attempt, len(_THREAD_OUTPUT_RETRY_DELAYS_S) - 1)
                ]
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                try:
                    await self._drain_thread_output_gate(gate)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempt += 1
                    if attempt <= len(_THREAD_OUTPUT_RETRY_DELAYS_S) or attempt % 12 == 0:
                        emit_event(
                            component="bridge",
                            event="bridge.thread_output_gate.delivery_retry_failed",
                            level="WARNING",
                            message="Buffered native output retry failed",
                            data={
                                "attempt": attempt,
                                "error_type": type(exc).__name__,
                                "thread_id": gate.thread_id,
                            },
                        )
                    continue
                return
        finally:
            if gate.retry_task is current_task:
                gate.retry_task = None

    async def _wait_for_thread_output_admission(self, sequence: int) -> None:
        async with self._thread_output_admission_condition:
            while (
                self._thread_output_admitted_sequence < sequence
                and not self._thread_handoff_closed
            ):
                await self._thread_output_admission_condition.wait()

    def _seal_thread_output_gate(self, gate: _ThreadOutputGate) -> None:
        sequence_reader = getattr(self.backend, "native_dispatch_sequence", None)
        if not callable(sequence_reader):
            return
        try:
            sequence = int(sequence_reader() or 0)
        except (TypeError, ValueError):
            return
        gate.release_through_sequence = max(gate.release_through_sequence, sequence)

    @staticmethod
    def _dispatch_sequence(payload: dict) -> int | None:
        try:
            sequence = int(payload.get("_imcodex_dispatch_sequence") or 0)
        except (TypeError, ValueError):
            return None
        return sequence if sequence > 0 else None

    async def _discard_thread_output_gate(
        self,
        gate: _ThreadOutputGate,
        *,
        note: str,
        process_buffered: bool = True,
    ) -> None:
        async with self._thread_output_gate_lock:
            if self._thread_output_gates_by_thread.get(gate.thread_id) is not gate:
                return
            buffered = list(gate.messages)
            self._remove_thread_output_gate_locked(gate)
        for buffered_message in buffered:
            if not process_buffered:
                self.store.update_native_appserver_event(
                    buffered_message.journal_sequence,
                    outcome="ingested",
                    note=note,
                )
                continue
            try:
                if buffered_message.kind == "notification":
                    await self._process_notification(
                        buffered_message.payload,
                        buffered_message.journal_sequence,
                    )
                else:
                    await self._process_server_request(
                        buffered_message.payload,
                        buffered_message.journal_sequence,
                    )
            except Exception as exc:
                self.store.update_native_appserver_event(
                    buffered_message.journal_sequence,
                    outcome="ingested",
                    note=note,
                )
                emit_event(
                    component="bridge",
                    event="bridge.thread_output_gate.discard_failed",
                    level="ERROR",
                    message="Buffered native message could not be reconciled after gate discard",
                    data={"error_type": type(exc).__name__, "thread_id": gate.thread_id},
                )

    def _remove_thread_output_gate_locked(self, gate: _ThreadOutputGate) -> None:
        gate.capacity_available.set()
        retry_task = gate.retry_task
        if retry_task is not None and retry_task is not asyncio.current_task():
            retry_task.cancel()
        gate.retry_task = None
        if self._thread_output_gates_by_thread.get(gate.thread_id) is gate:
            self._thread_output_gates_by_thread.pop(gate.thread_id, None)
        route = (gate.channel_id, gate.conversation_id)
        if self._thread_output_gates_by_route.get(route) is gate:
            self._thread_output_gates_by_route.pop(route, None)

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(value) if value is not None else 0
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _thread_status_is_active(status: str) -> bool:
        return str(status or "").strip().lower() in _ACTIVE_THREAD_STATUSES

    @staticmethod
    def _clone_outbound_message(message: OutboundMessage) -> OutboundMessage:
        return replace(message, metadata=dict(message.metadata))

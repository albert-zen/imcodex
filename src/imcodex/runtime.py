from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from .observability.runtime import mark_http_health


OBSERVABILITY_IO_TIMEOUT_S = 4.0


@dataclass(slots=True)
class AppRuntime:
    client: object
    service: object
    managed_channels: list[object] = field(default_factory=list)
    observability: object | None = None

    async def start(self) -> None:
        try:
            if self.observability is not None:
                self.observability.start()
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.starting",
                )
            self.client.add_notification_handler(self.service.handle_notification)
            self.client.add_server_request_handler(self.service.handle_server_request)
            reset_hook = getattr(self.client, "add_connection_reset_handler", None)
            if callable(reset_hook):
                reset_hook(self.service.handle_connection_reset)
            ready_hook = getattr(self.client, "add_connection_ready_handler", None)
            backend = getattr(self.service, "backend", None)
            ensure_permission_defaults = getattr(backend, "ensure_default_permission_mode", None)
            service_ready = getattr(self.service, "handle_connection_ready", None)
            if callable(ready_hook):
                if callable(ensure_permission_defaults):
                    ready_hook(ensure_permission_defaults)
                if callable(service_ready):
                    ready_hook(service_ready)
            for channel in self._managed_channels_in_order():
                await channel.start()
            # Outbound adapters must be prepared before native rehydration can
            # replay approvals or recover a terminal result. Inbound work that
            # arrives during this short window shares the client's serialized
            # initialize path.
            await self.client.initialize()
            if self.observability is not None:
                self._observe(mark_http_health, listening=True)
                self._observe(self.observability.update_health, status="healthy")
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.started",
                )
                flush_observability = getattr(self.observability, "flush", None)
                if callable(flush_observability):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.to_thread(flush_observability),
                            timeout=OBSERVABILITY_IO_TIMEOUT_S,
                        )
        except BaseException as exc:
            await self._rollback_start()
            if self.observability is not None:
                self._observe(self.observability.update_health, status="unhealthy")
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.start_failed",
                    level="ERROR",
                    message=str(exc),
                    data={"error_type": type(exc).__name__},
                )
                await self._stop_observability()
            raise

    async def stop(self) -> None:
        cleanup = asyncio.create_task(self._stop_resources())
        cancelled = await self._wait_for_cleanup(cleanup)
        if cancelled:
            raise asyncio.CancelledError

    async def _rollback_start(self) -> None:
        async def cleanup() -> None:
            for channel in self._managed_channels_in_reverse():
                with contextlib.suppress(BaseException):
                    await channel.stop()
            with contextlib.suppress(BaseException):
                await self._close_service()
            with contextlib.suppress(BaseException):
                await self.client.close()
            with contextlib.suppress(BaseException):
                await self._flush_store_writes()

        task = asyncio.create_task(cleanup())
        await self._wait_for_cleanup(task, suppress_result=True)

    async def _stop_resources(self) -> None:
        errors: list[Exception] = []
        if self.observability is not None:
            self._observe(
                self.observability.emit_event,
                component="bridge",
                event="bridge.stopping",
            )
        for channel in self._managed_channels_in_reverse():
            try:
                await channel.stop()
            except asyncio.CancelledError:
                errors.append(RuntimeError("channel shutdown was cancelled"))
            except Exception as exc:
                errors.append(exc)
        try:
            await self._close_service()
        except asyncio.CancelledError:
            errors.append(RuntimeError("bridge service shutdown was cancelled"))
        except Exception as exc:
            errors.append(exc)
        try:
            await self.client.close()
        except asyncio.CancelledError:
            errors.append(RuntimeError("app-server client shutdown was cancelled"))
        except Exception as exc:
            errors.append(exc)
        try:
            await self._flush_store_writes()
        except asyncio.CancelledError:
            errors.append(RuntimeError("bridge state flush was cancelled"))
        except Exception as exc:
            errors.append(exc)
        finally:
            if self.observability is not None:
                self._observe(mark_http_health, listening=False)
                self._observe(self.observability.update_health, status="stopped")
                self._observe(
                    self.observability.emit_event,
                    component="bridge",
                    event="bridge.stopped",
                )
                await self._stop_observability()
        if errors:
            raise ExceptionGroup("runtime shutdown failed", errors)

    def _managed_channels_in_reverse(self) -> list[object]:
        channels: list[object] = []
        seen: set[int] = set()
        for channel in reversed(self.managed_channels):
            identity = id(channel)
            if identity in seen:
                continue
            seen.add(identity)
            channels.append(channel)
        return channels

    def _managed_channels_in_order(self) -> list[object]:
        channels: list[object] = []
        seen: set[int] = set()
        for channel in self.managed_channels:
            identity = id(channel)
            if identity in seen:
                continue
            seen.add(identity)
            channels.append(channel)
        return channels

    async def _flush_store_writes(self) -> None:
        store = getattr(self.service, "store", None)
        flush = getattr(store, "flush_pending_writes", None)
        if callable(flush):
            await flush()

    async def _close_service(self) -> None:
        close = getattr(self.service, "close", None)
        if callable(close):
            await close()

    async def _stop_observability(self) -> None:
        stop = getattr(self.observability, "stop", None)
        if not callable(stop):
            return
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(
                asyncio.to_thread(stop),
                timeout=OBSERVABILITY_IO_TIMEOUT_S,
            )

    @staticmethod
    async def _wait_for_cleanup(
        task: asyncio.Task,
        *,
        suppress_result: bool = False,
    ) -> bool:
        """Wait through repeated caller cancellation without cancelling cleanup."""

        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                if not suppress_result and not cancelled:
                    raise
                break
        if suppress_result or cancelled:
            with contextlib.suppress(BaseException):
                task.result()
        else:
            task.result()
        return cancelled

    @staticmethod
    def _observe(callback, *args, **kwargs) -> None:
        with contextlib.suppress(Exception):
            callback(*args, **kwargs)

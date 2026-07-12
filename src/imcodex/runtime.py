from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from .observability.runtime import mark_http_health


@dataclass(slots=True)
class AppRuntime:
    client: object
    service: object
    managed_channels: list[object] = field(default_factory=list)
    observability: object | None = None

    async def start(self) -> None:
        started_channels: list[object] = []
        client_initialized = False
        if self.observability is not None:
            self.observability.start()
            self.observability.emit_event(component="bridge", event="bridge.starting")
        try:
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
            await self.client.initialize()
            client_initialized = True
            for channel in self.managed_channels:
                started_channels.append(channel)
                await channel.start()
        except Exception as exc:
            for channel in reversed(started_channels):
                with contextlib.suppress(Exception):
                    await channel.stop()
            if client_initialized:
                with contextlib.suppress(Exception):
                    await self.client.close()
            if self.observability is not None:
                self.observability.update_health(status="unhealthy")
                self.observability.emit_event(
                    component="bridge",
                    event="bridge.start_failed",
                    level="ERROR",
                    message=str(exc),
                    data={"error_type": type(exc).__name__},
                )
                self.observability.stop()
            raise
        if self.observability is not None:
            mark_http_health(listening=True)
            self.observability.update_health(status="healthy")
            self.observability.emit_event(component="bridge", event="bridge.started")

    async def stop(self) -> None:
        errors: list[Exception] = []
        if self.observability is not None:
            self.observability.emit_event(component="bridge", event="bridge.stopping")
        for channel in reversed(self.managed_channels):
            try:
                await channel.stop()
            except Exception as exc:
                errors.append(exc)
        try:
            await self.client.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            if self.observability is not None:
                mark_http_health(listening=False)
                self.observability.update_health(status="stopped")
                self.observability.emit_event(component="bridge", event="bridge.stopped")
                self.observability.stop()
        if errors:
            raise ExceptionGroup("runtime shutdown failed", errors)

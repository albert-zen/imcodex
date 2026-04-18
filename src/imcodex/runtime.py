from __future__ import annotations

from dataclasses import dataclass, field

from .observability.runtime import mark_http_health


@dataclass(slots=True)
class AppRuntime:
    client: object
    service: object
    managed_channels: list[object] = field(default_factory=list)
    observability: object | None = None

    async def start(self) -> None:
        if self.observability is not None:
            self.observability.start()
            self.observability.emit_event(component="bridge", event="bridge.starting")
        try:
            self.client.add_notification_handler(self.service.handle_notification)
            self.client.add_server_request_handler(self.service.handle_server_request)
            await self.client.connect()
            for channel in self.managed_channels:
                await channel.start()
        except Exception as exc:
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
        if self.observability is not None:
            self.observability.emit_event(component="bridge", event="bridge.stopping")
        for channel in reversed(self.managed_channels):
            await channel.stop()
        await self.client.close()
        if self.observability is not None:
            mark_http_health(listening=False)
            self.observability.update_health(status="stopped")
            self.observability.emit_event(component="bridge", event="bridge.stopped")
            self.observability.stop()

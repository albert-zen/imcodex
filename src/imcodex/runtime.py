from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AppRuntime:
    supervisor: object
    client: object
    service: object
    managed_channels: list[object] = field(default_factory=list)

    async def start(self) -> None:
        await self.supervisor.start()
        self.client.add_notification_handler(self.service.handle_notification)
        self.client.add_server_request_handler(self.service.handle_server_request)
        await self.client.connect()
        await self.client.initialize()
        for channel in self.managed_channels:
            await channel.start()

    async def stop(self) -> None:
        for channel in reversed(self.managed_channels):
            await channel.stop()
        await self.client.close()
        await self.supervisor.stop()

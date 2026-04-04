from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AppRuntime:
    supervisor: object
    client: object
    service: object

    async def start(self) -> None:
        await self.supervisor.start()
        self.client.add_notification_handler(self.service.handle_notification)
        self.client.add_server_request_handler(self.service.handle_server_request)
        await self.client.connect()
        await self.client.initialize()

    async def stop(self) -> None:
        await self.client.close()
        await self.supervisor.stop()

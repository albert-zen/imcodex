from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Any


SpawnProcess = Callable[..., Awaitable[Any]]
ProbeReady = Callable[[str], Awaitable[object]]


@dataclass(slots=True)
class AppServerSupervisor:
    port: int
    codex_bin: str = "codex"
    host: str = "127.0.0.1"
    spawn_process: SpawnProcess | None = None
    probe_ready: ProbeReady | None = None
    ready_retries: int = 20
    ready_interval_s: float = 0.1
    _process: Any = None

    @property
    def ready_url(self) -> str:
        return f"http://{self.host}:{self.port}/readyz"

    def build_command(self) -> list[str]:
        return [
            self.codex_bin,
            "app-server",
            "--listen",
            f"ws://{self.host}:{self.port}",
        ]

    def is_ready(self, probe_result: object) -> bool:
        if isinstance(probe_result, bool):
            return probe_result
        if isinstance(probe_result, int):
            return probe_result == 200
        text = str(probe_result).strip().lower()
        return text.startswith("200") or "200 ok" in text

    async def start(self) -> None:
        if self._process is not None:
            return
        spawn = self.spawn_process or self._default_spawn
        self._process = await spawn(*self.build_command())
        await self._wait_until_ready()

    async def stop(self) -> None:
        if self._process is None:
            return
        terminate = getattr(self._process, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(self._process, "wait", None)
        if callable(wait):
            await wait()
        self._process = None

    async def _wait_until_ready(self) -> None:
        probe = self.probe_ready or self._default_probe
        for _ in range(self.ready_retries):
            if self.is_ready(await probe(self.ready_url)):
                return
            await asyncio.sleep(self.ready_interval_s)
        raise TimeoutError(f"codex app-server did not become ready at {self.ready_url}")

    async def _default_spawn(self, *command: str):
        return await asyncio.create_subprocess_exec(*command)

    async def _default_probe(self, url: str) -> object:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(url)
        return response.status_code

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Awaitable, Callable, Any

import httpx


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
        resolved = self._resolve_command_for_spawn(command)
        return await asyncio.create_subprocess_exec(*resolved)

    async def _default_probe(self, url: str) -> object:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url)
                return response.status_code
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                parsed = urlparse(url)
                if not parsed.hostname or not parsed.port:
                    raise
                _, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
                writer.close()
                await writer.wait_closed()
                return True

    def _resolve_command_for_spawn(self, command: tuple[str, ...]) -> tuple[str, ...]:
        if os.name != "nt" or not command:
            return command
        executable = command[0]
        if any(sep in executable for sep in ("\\", "/")):
            if executable.lower().endswith(".cmd"):
                return ("cmd.exe", "/c", executable, *command[1:])
            return command
        if "." in executable:
            return command
        shim = shutil.which(f"{executable}.cmd")
        if shim:
            return ("cmd.exe", "/c", shim, *command[1:])
        resolved = shutil.which(f"{executable}.exe")
        if resolved:
            return (resolved, *command[1:])
        return command

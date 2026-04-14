from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


SpawnProcess = Callable[..., Awaitable[Any] | Any]


@dataclass(slots=True)
class AppServerSupervisor:
    codex_bin: str = "codex"
    spawn_process: SpawnProcess | None = None
    _process: Any = None

    def build_command(self) -> list[str]:
        return [self.codex_bin, "app-server", "--listen", "stdio://"]

    @property
    def process(self) -> Any | None:
        return self._process

    async def start(self) -> Any:
        if self._process is not None and getattr(self._process, "returncode", None) is None:
            return self._process
        spawn = self.spawn_process or self._default_spawn
        process = spawn(*self.build_command())
        if inspect.isawaitable(process):
            process = await process
        self._process = process
        return process

    async def stop(self) -> None:
        if self._process is None:
            return
        terminate = getattr(self._process, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(self._process, "wait", None)
        if callable(wait):
            result = wait()
            if inspect.isawaitable(result):
                await result
        self._process = None

    async def _default_spawn(self, *command: str) -> Any:
        resolved = self._resolve_command_for_spawn(command)
        return await asyncio.create_subprocess_exec(
            *resolved,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

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

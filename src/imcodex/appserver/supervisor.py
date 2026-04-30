from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import websockets


SpawnProcess = Callable[..., Awaitable[Any] | Any]
ConnectWebSocket = Callable[..., Awaitable[Any] | Any]
STDIO_STREAM_LIMIT = 1024 * 1024
WS_MAX_SIZE = 16 * 1024 * 1024


@dataclass(slots=True)
class AppServerSupervisor:
    codex_bin: str = "codex"
    app_server_url: str | None = None
    core_mode: str = "auto"
    core_url: str | None = None
    spawn_process: SpawnProcess | None = None
    websocket_factory: ConnectWebSocket | None = None
    shared_app_server_url: str = "ws://127.0.0.1:8765"
    _process: Any = None
    _connection_mode: str = "disconnected"

    def build_command(self) -> list[str]:
        return [self.codex_bin, "app-server", "--listen", "stdio://"]

    @property
    def process(self) -> Any | None:
        return self._process

    @property
    def connection_mode(self) -> str:
        return self._connection_mode

    async def connect_shared(self) -> Any | None:
        if self.core_mode == "spawned-stdio":
            return None

        async def _default_connect(url: str) -> Any:
            return await websockets.connect(url, max_size=WS_MAX_SIZE)

        connect = self.websocket_factory or _default_connect
        for url in self._shared_candidates():
            try:
                connection = connect(url)
                if inspect.isawaitable(connection):
                    connection = await connection
            except Exception:
                continue
            self._connection_mode = self._websocket_connection_mode()
            return connection
        return None

    @property
    def allow_spawn_fallback(self) -> bool:
        return self.core_mode in {"spawned-stdio", "auto"}

    async def start(self) -> Any:
        if self._process is not None and getattr(self._process, "returncode", None) is None:
            self._connection_mode = "spawned-stdio"
            return self._process
        spawn = self.spawn_process or self._default_spawn
        process = spawn(*self.build_command())
        if inspect.isawaitable(process):
            process = await process
        self._process = process
        self._connection_mode = "spawned-stdio"
        return process

    async def stop(self) -> None:
        if self._process is None:
            self._connection_mode = "disconnected"
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
        self._connection_mode = "disconnected"

    async def _default_spawn(self, *command: str) -> Any:
        resolved = self._resolve_command_for_spawn(command)
        return await asyncio.create_subprocess_exec(
            *resolved,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDIO_STREAM_LIMIT,
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

    def _shared_candidates(self) -> list[str]:
        if self.core_mode == "dedicated-ws":
            return [self.core_url or self.app_server_url or self.shared_app_server_url]
        if self.core_mode == "shared-ws":
            return [self.app_server_url or self.core_url or self.shared_app_server_url]
        if self.core_mode == "auto":
            if self.core_url or self.app_server_url:
                return [self.core_url or self.app_server_url or self.shared_app_server_url]
            return [self.shared_app_server_url]
        return []

    def _websocket_connection_mode(self) -> str:
        if self.core_mode == "dedicated-ws":
            return "dedicated-ws"
        return "shared-ws"

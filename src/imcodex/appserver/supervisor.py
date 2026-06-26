from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Awaitable, Callable

import websockets

from ..observability.runtime import emit_event
from .retry import RetryBackoff


SpawnProcess = Callable[..., Awaitable[Any] | Any]
ConnectWebSocket = Callable[..., Awaitable[Any] | Any]
Sleep = Callable[[float], Awaitable[None] | None]
STDIO_STREAM_LIMIT = 1024 * 1024
WS_MAX_SIZE = 16 * 1024 * 1024
DEFAULT_HEALTH_PATHS = ("/readyz", "/healthz")


@dataclass(frozen=True, slots=True)
class HealthProbeResult:
    ok: bool
    url: str | None = None
    status_code: int | None = None
    error_type: str | None = None
    message: str | None = None

    def to_diagnostic(self) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {"health_ok": self.ok}
        if self.url is not None:
            diagnostic["health_url"] = _safe_url_label(self.url)
        if self.status_code is not None:
            diagnostic["health_status_code"] = self.status_code
        if self.error_type is not None:
            diagnostic["health_error_type"] = self.error_type
        if self.message is not None:
            diagnostic["health_message"] = self.message
        return diagnostic


HealthProbe = Callable[[list[str], dict[str, str], float], Awaitable[HealthProbeResult] | HealthProbeResult]


def derive_health_probe_urls(endpoint_url: str) -> list[str]:
    parsed = urlsplit(endpoint_url)
    scheme = {
        "ws": "http",
        "wss": "https",
        "http": "http",
        "https": "https",
    }.get(parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        return []
    return [urlunsplit((scheme, parsed.netloc, path, "", "")) for path in DEFAULT_HEALTH_PATHS]


async def default_health_probe(
    urls: list[str],
    headers: dict[str, str],
    timeout_s: float,
) -> HealthProbeResult:
    return await asyncio.to_thread(_default_health_probe_sync, urls, headers, timeout_s)


def _default_health_probe_sync(
    urls: list[str],
    headers: dict[str, str],
    timeout_s: float,
) -> HealthProbeResult:
    last_result = HealthProbeResult(ok=False, message="no health probe URLs")
    for url in urls:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, timeout_s)) as response:
                status_code = int(getattr(response, "status", response.getcode()))
                if 200 <= status_code < 400:
                    return HealthProbeResult(ok=True, url=url, status_code=status_code)
                last_result = HealthProbeResult(
                    ok=False,
                    url=url,
                    status_code=status_code,
                    message=f"HTTP {status_code}",
                )
        except urllib.error.HTTPError as exc:
            last_result = HealthProbeResult(
                ok=False,
                url=url,
                status_code=exc.code,
                error_type=type(exc).__name__,
                message=f"HTTP {exc.code}",
            )
        except Exception as exc:
            last_result = HealthProbeResult(
                ok=False,
                url=url,
                error_type=type(exc).__name__,
                message=str(exc) or type(exc).__name__,
            )
    return last_result


@dataclass(slots=True)
class AppServerSupervisor:
    codex_bin: str = "codex"
    app_server_url: str | None = None
    core_mode: str = "auto"
    core_url: str | None = None
    app_server_auth_token: str | None = None
    app_server_auth_token_file: str | os.PathLike[str] | None = None
    websocket_retry_policy: RetryBackoff = field(default_factory=RetryBackoff)
    websocket_open_timeout_s: float = 3.0
    health_probe_timeout_s: float = 1.0
    spawn_process: SpawnProcess | None = None
    websocket_factory: ConnectWebSocket | None = None
    health_probe: HealthProbe | None = None
    sleep: Sleep | None = None
    random_float: Callable[[], float] | None = None
    shared_app_server_url: str = "ws://127.0.0.1:8765"
    _process: Any = None
    _connection_mode: str = "disconnected"
    _last_connect_diagnostic: dict[str, Any] | None = None

    def build_command(self) -> list[str]:
        return [self.codex_bin, "app-server", "--listen", "stdio://"]

    @property
    def process(self) -> Any | None:
        return self._process

    @property
    def connection_mode(self) -> str:
        return self._connection_mode

    @property
    def last_connect_diagnostic(self) -> dict[str, Any] | None:
        return dict(self._last_connect_diagnostic or {}) or None

    def websocket_headers(self) -> dict[str, str]:
        token = self._resolve_bearer_token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def connect_shared(self) -> Any | None:
        self._last_connect_diagnostic = None
        if self.core_mode == "spawned-stdio":
            return None
        headers = self.websocket_headers()
        connect = self.websocket_factory or _default_connect
        for url in self._shared_candidates():
            connection = await self._connect_with_retries(connect, url, headers)
            if connection is None:
                if self._should_probe_health():
                    await self._probe_health(url, headers)
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
        process = self._process
        try:
            if getattr(process, "returncode", None) is None:
                terminate = getattr(process, "terminate", None)
                if callable(terminate):
                    try:
                        terminate()
                    except ProcessLookupError:
                        pass
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    result = wait()
                    if inspect.isawaitable(result):
                        await result
                except ProcessLookupError:
                    pass
        finally:
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

    async def _connect_with_retries(
        self,
        connect: ConnectWebSocket,
        url: str,
        headers: dict[str, str],
    ) -> Any | None:
        attempts = self.websocket_retry_policy.attempts
        for attempt in range(1, attempts + 1):
            try:
                connection = self._open_websocket(connect, url, headers)
                if inspect.isawaitable(connection):
                    connection = await connection
                return connection
            except Exception as exc:
                self._record_websocket_connect_failure(url=url, attempt=attempt, attempts=attempts, exc=exc)
                if attempt >= attempts:
                    return None
                delay_s = self.websocket_retry_policy.delay_after_failure(
                    attempt,
                    random_float=self.random_float,
                )
                emit_event(
                    component="appserver.supervisor",
                    event="appserver.connect.websocket_retry_scheduled",
                    level="WARNING",
                    message="Retrying websocket app-server connection",
                    data={
                        "url": _safe_url_label(url),
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                        "delay_s": round(delay_s, 3),
                    },
                )
                await self._sleep(delay_s)
        return None

    def _open_websocket(
        self,
        connect: ConnectWebSocket,
        url: str,
        headers: dict[str, str],
    ) -> Awaitable[Any] | Any:
        if self.websocket_factory is None:
            kwargs: dict[str, Any] = {
                "max_size": WS_MAX_SIZE,
                "open_timeout": self.websocket_open_timeout_s,
            }
            if headers:
                kwargs["additional_headers"] = headers
            return websockets.connect(url, **kwargs)
        if headers:
            return connect(url, additional_headers=headers)
        return connect(url)

    async def _probe_health(self, url: str, headers: dict[str, str]) -> HealthProbeResult | None:
        urls = derive_health_probe_urls(url)
        if not urls:
            return None
        probe = self.health_probe or default_health_probe
        try:
            result = probe(urls, headers, self.health_probe_timeout_s)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            result = HealthProbeResult(
                ok=False,
                url=urls[0],
                error_type=type(exc).__name__,
                message=str(exc) or type(exc).__name__,
            )
        diagnostic = result.to_diagnostic()
        if self._last_connect_diagnostic is None:
            self._last_connect_diagnostic = {"url": _safe_url_label(url)}
        self._last_connect_diagnostic.update(diagnostic)
        emit_event(
            component="appserver.supervisor",
            event="appserver.connect.health_probe_succeeded" if result.ok else "appserver.connect.health_probe_failed",
            level="INFO" if result.ok else "WARNING",
            message="App-server health probe succeeded" if result.ok else "App-server health probe failed",
            data={"url": _safe_url_label(url), **diagnostic},
        )
        return result

    def _record_websocket_connect_failure(
        self,
        *,
        url: str,
        attempt: int,
        attempts: int,
        exc: Exception,
    ) -> None:
        diagnostic = {
            "url": _safe_url_label(url),
            "attempt": attempt,
            "max_attempts": attempts,
            "error_type": type(exc).__name__,
        }
        self._last_connect_diagnostic = diagnostic
        emit_event(
            component="appserver.supervisor",
            event="appserver.connect.websocket_failed",
            level="WARNING",
            message="Websocket app-server connection failed",
            data=diagnostic,
        )

    async def _sleep(self, delay_s: float) -> None:
        sleeper = self.sleep or asyncio.sleep
        result = sleeper(max(0.0, delay_s))
        if inspect.isawaitable(result):
            await result

    def _resolve_bearer_token(self) -> str | None:
        if self.app_server_auth_token and self.app_server_auth_token.strip():
            return self.app_server_auth_token.strip()
        if self.app_server_auth_token_file is None:
            return None
        path = Path(self.app_server_auth_token_file)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"app-server auth token file is empty: {path}")
        return token

    def _should_probe_health(self) -> bool:
        return self.core_mode in {"dedicated-ws", "shared-ws"}


async def _default_connect(url: str, **kwargs: Any) -> Any:
    return await websockets.connect(url, **kwargs)


def _safe_url_label(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

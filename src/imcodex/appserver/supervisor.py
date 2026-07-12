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
from ..app_server_target import (
    SPAWNED_STDIO_CONNECTION_MODE,
    AppServerTarget,
    resolve_app_server_target,
)


SpawnProcess = Callable[..., Awaitable[Any] | Any]
ConnectWebSocket = Callable[..., Awaitable[Any] | Any]
ConnectUnixWebSocket = Callable[..., Awaitable[Any] | Any]
Sleep = Callable[[float], Awaitable[None] | None]
STDIO_STREAM_LIMIT = 1024 * 1024
# Native thread/resume may legitimately return the complete thread in one
# WebSocket frame. A bridge-side cap can trap large threads in a reconnect loop
# before the response can be normalized, so trust the explicitly configured
# App Server endpoint and let JSON decoding provide the natural memory bound.
WS_MAX_SIZE: int | None = None
DEFAULT_HEALTH_PATHS = ("/readyz", "/healthz")
DEFAULT_UNIX_WEBSOCKET_URI = "ws://localhost/"
UNIX_ENDPOINT_PREFIX = "unix://"


class UnsupportedUnixSocketError(OSError):
    pass


def default_app_server_control_socket(codex_home: str | os.PathLike[str] | None = None) -> Path:
    configured_home = os.fspath(codex_home) if codex_home is not None else os.getenv("CODEX_HOME")
    if configured_home:
        home = Path(configured_home)
        if not home.exists():
            raise FileNotFoundError(f"CODEX_HOME does not exist: {home}")
        if not home.is_dir():
            raise NotADirectoryError(f"CODEX_HOME is not a directory: {home}")
        home = home.resolve(strict=True)
    else:
        home = Path.home() / ".codex"
    return home / "app-server-control" / "app-server-control.sock"


def resolve_unix_socket_path(
    endpoint_url: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Path:
    if not endpoint_url.startswith(UNIX_ENDPOINT_PREFIX):
        raise ValueError(f"not a unix app-server endpoint: {endpoint_url}")
    raw_path = endpoint_url[len(UNIX_ENDPOINT_PREFIX) :]
    if not raw_path:
        return default_app_server_control_socket(codex_home)
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _is_unix_endpoint(endpoint_url: str) -> bool:
    return endpoint_url.startswith(UNIX_ENDPOINT_PREFIX)


def _validate_unix_endpoint(endpoint_url: str) -> None:
    if endpoint_url.lower().startswith(UNIX_ENDPOINT_PREFIX) and not _is_unix_endpoint(endpoint_url):
        raise ValueError("unix app-server endpoints must use the lowercase unix:// prefix")
    if not _is_unix_endpoint(endpoint_url):
        return
    if os.name == "nt":
        raise UnsupportedUnixSocketError(
            "unix app-server endpoints are not supported on native Windows; "
            "use an explicit ws:// endpoint or WSL"
        )
    resolve_unix_socket_path(endpoint_url)


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
    core_mode: str | None = None
    core_url: str | None = None
    app_server_auth_token: str | None = None
    app_server_auth_token_file: str | os.PathLike[str] | None = None
    websocket_retry_policy: RetryBackoff = field(default_factory=RetryBackoff)
    websocket_open_timeout_s: float = 3.0
    health_probe_timeout_s: float = 1.0
    spawn_process: SpawnProcess | None = None
    websocket_factory: ConnectWebSocket | None = None
    unix_websocket_factory: ConnectUnixWebSocket | None = None
    health_probe: HealthProbe | None = None
    sleep: Sleep | None = None
    random_float: Callable[[], float] | None = None
    _process: Any = None
    _connection_mode: str = "disconnected"
    _last_connect_diagnostic: dict[str, Any] | None = None
    _target: AppServerTarget = field(init=False)

    def __post_init__(self) -> None:
        self._target = resolve_app_server_target(
            app_server_url=self.app_server_url,
            core_url=self.core_url,
            core_mode=self.core_mode,
        )
        self.app_server_url = self._target.endpoint

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

    @property
    def target(self) -> AppServerTarget:
        return self._target

    @property
    def connection_target(self) -> str:
        return self._target.endpoint

    @property
    def display_connection_target(self) -> str:
        return _safe_url_label(self._target.endpoint)

    @property
    def supports_background_reconnect(self) -> bool:
        return self._target.preserves_server_state

    @property
    def spawns_stdio(self) -> bool:
        return not self._target.is_external

    def websocket_headers(self) -> dict[str, str]:
        token = self._resolve_bearer_token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def connect_external(self) -> Any | None:
        self._last_connect_diagnostic = None
        if not self._target.is_external:
            return None
        headers = self.websocket_headers()
        connect = self.websocket_factory or _default_connect
        url = self._target.endpoint
        try:
            _validate_unix_endpoint(url)
        except (ValueError, OSError) as exc:
            self._last_connect_diagnostic = {
                "url": _safe_url_label(url),
                "error_type": type(exc).__name__,
            }
            raise
        connection = await self._connect_with_retries(connect, url, headers)
        if connection is None:
            if self._should_probe_health():
                await self._probe_health(url, headers)
            return None
        self._connection_mode = self._target.connection_mode
        return connection

    async def start(self) -> Any:
        if not self.spawns_stdio:
            raise RuntimeError("external App Server lifecycle is not owned by the bridge")
        if self._process is not None and getattr(self._process, "returncode", None) is None:
            self._connection_mode = SPAWNED_STDIO_CONNECTION_MODE
            return self._process
        spawn = self.spawn_process or self._default_spawn
        process = spawn(*self.build_command())
        if inspect.isawaitable(process):
            process = await process
        self._process = process
        self._connection_mode = SPAWNED_STDIO_CONNECTION_MODE
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
        if _is_unix_endpoint(url):
            path = resolve_unix_socket_path(url)
            connect_unix = self.unix_websocket_factory or websockets.unix_connect
            kwargs: dict[str, Any] = {
                "uri": DEFAULT_UNIX_WEBSOCKET_URI,
                "compression": None,
                "max_size": WS_MAX_SIZE,
                "open_timeout": self.websocket_open_timeout_s,
            }
            if headers:
                kwargs["additional_headers"] = headers
            return connect_unix(str(path), **kwargs)
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
        return self._target.transport == "tcp-websocket"


async def _default_connect(url: str, **kwargs: Any) -> Any:
    return await websockets.connect(url, **kwargs)


def _safe_url_label(url: str) -> str:
    if _is_unix_endpoint(url):
        return url
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

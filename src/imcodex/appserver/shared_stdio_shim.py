"""Stateless JSONL stdio adapter for a shared native Codex App Server.

T3 Code's Codex provider launches ``<binary> app-server`` and speaks one JSON
object per stdio line. A shared native App Server instead exposes JSON-RPC as
one text message per WebSocket frame. This module translates that transport
shape and, only in explicit T3 chat-sync mode, locally resolves the one known
process-global MCP reload. It owns no lifecycle state.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from websockets.asyncio.client import unix_connect

from .supervisor import resolve_unix_socket_path

DEFAULT_ENDPOINT = "unix://"
ENDPOINT_ENV = "IMCODEX_SHARED_APP_SERVER_URL"
CHAT_SYNC_WITHOUT_T3_MCP_ARG = "--chat-sync-without-t3-mcp"
# Match the native stdio transport's practical absence of an application-level
# line cap. The complete JSON value must still fit in memory before it can
# become one WebSocket text frame.
STDIN_READER_LIMIT = 2**31 - 1
WEBSOCKET_URI = "ws://localhost/"
_T3_URL_CONFIG_PREFIX = "mcp_servers.t3-code.url="
_T3_BEARER_CONFIG = (
    'mcp_servers.t3-code.bearer_token_env_var="T3_MCP_BEARER_TOKEN"'
)


class ShimUsageError(ValueError):
    """A fixed, credential-safe command-line error."""


class ShimTransportError(RuntimeError):
    """A credential-safe transport contract error."""


@dataclass(frozen=True, slots=True)
class SharedAppServerShimConfig:
    endpoint: str
    socket_path: Path
    suppress_t3_mcp_reload: bool = False


def _t3_config_kind(value: str) -> str | None:
    if value.startswith(_T3_URL_CONFIG_PREFIX) and value != _T3_URL_CONFIG_PREFIX:
        return "url"
    if value == _T3_BEARER_CONFIG:
        return "bearer"
    return None


def _consume_compatibility_args(arguments: Sequence[str]) -> tuple[int, int]:
    """Recognize only T3's known per-session MCP flags.

    A shared App Server owns process configuration, so these child-process
    flags cannot alter it. Returning exact key counts lets the caller require
    the explicit chat-sync guard before opening the shared connection.
    """

    url_count = 0
    bearer_count = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-c", "--config"}:
            index += 1
            if index >= len(arguments) or _t3_config_kind(arguments[index]) is None:
                raise ShimUsageError("unsupported shared App Server child configuration")
            value = arguments[index]
        elif argument.startswith(("-c=", "--config=")):
            _, value = argument.split("=", 1)
            if _t3_config_kind(value) is None:
                raise ShimUsageError("unsupported shared App Server child configuration")
        else:
            raise ShimUsageError("unsupported shared App Server shim argument")
        if _t3_config_kind(value) == "url":
            url_count += 1
        else:
            bearer_count += 1
        index += 1
    return url_count, bearer_count


def parse_shim_args(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> SharedAppServerShimConfig:
    source = os.environ if environ is None else environ
    arguments = list(argv)
    if not arguments or arguments.pop(0) != "app-server":
        raise ShimUsageError("expected the T3 '<binary> app-server' launch shape")

    endpoint = str(source.get(ENDPOINT_ENV, "")).strip() or DEFAULT_ENDPOINT
    chat_sync_without_t3_mcp = False
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--connect":
            index += 1
            if index >= len(arguments):
                raise ShimUsageError("--connect requires a Unix WebSocket endpoint")
            endpoint = arguments[index].strip()
        elif argument.startswith("--connect="):
            endpoint = argument.split("=", 1)[1].strip()
        elif argument == CHAT_SYNC_WITHOUT_T3_MCP_ARG:
            chat_sync_without_t3_mcp = True
        else:
            remaining.append(argument)
            if argument in {"-c", "--config"} and index + 1 < len(arguments):
                index += 1
                remaining.append(arguments[index])
        index += 1

    t3_url_count, t3_bearer_count = _consume_compatibility_args(remaining)
    has_t3_mcp_config = bool(t3_url_count or t3_bearer_count)
    if has_t3_mcp_config and not chat_sync_without_t3_mcp:
        raise ShimUsageError(
            "T3 per-session MCP configuration and reload behavior are unsafe on a shared "
            f"App Server; use {CHAT_SYNC_WITHOUT_T3_MCP_ARG} only for chat sync without T3 MCP"
        )
    if chat_sync_without_t3_mcp and (t3_url_count, t3_bearer_count) not in {
        (0, 0),
        (1, 1),
    }:
        raise ShimUsageError(
            "chat sync without T3 MCP accepts either the T3 provider probe without "
            "per-session config or exactly one known T3 URL and bearer config"
        )
    if not endpoint.startswith("unix://"):
        raise ShimUsageError("the shared App Server shim requires a unix:// endpoint")
    try:
        socket_path = resolve_unix_socket_path(endpoint, codex_home=source.get("CODEX_HOME"))
    except (OSError, ValueError) as exc:
        raise ShimUsageError("invalid shared App Server Unix endpoint") from exc
    return SharedAppServerShimConfig(
        endpoint=endpoint,
        socket_path=socket_path,
        suppress_t3_mcp_reload=chat_sync_without_t3_mcp,
    )


async def _open_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=STDIN_READER_LIMIT)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)

    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        sys.stdout.buffer,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)
    return reader, writer


def _frame_from_line(line: bytes) -> str:
    if line.endswith(b"\n"):
        line = line[:-1]
    if line.endswith(b"\r"):
        line = line[:-1]
    try:
        return line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShimTransportError("stdin contained non-UTF-8 protocol bytes") from exc


def _intercept_t3_mcp_reload(frame: str) -> str | None:
    try:
        message = json.loads(frame)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    if method != "config/mcpServer/reload":
        if isinstance(method, str) and (
            method.startswith(("config/", "account/login", "account/logout"))
        ):
            raise ShimTransportError("chat-sync mode rejected a process-global mutation")
        return None
    request_id = message.get("id")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        raise ShimTransportError("T3 MCP reload request omitted a supported request id")
    return json.dumps({"id": request_id, "result": {}}, separators=(",", ":"))


async def relay_stdio_to_unix_websocket(
    config: SharedAppServerShimConfig,
    *,
    reader: asyncio.StreamReader | None = None,
    writer: asyncio.StreamWriter | None = None,
    connect: Any = unix_connect,
) -> None:
    if (reader is None) != (writer is None):
        raise ValueError("reader and writer must be supplied together")
    owned_stdio = reader is None
    if owned_stdio:
        reader, writer = await _open_stdio()
    assert reader is not None
    assert writer is not None

    try:
        async with connect(
            str(config.socket_path),
            uri=WEBSOCKET_URI,
            compression=None,
            max_size=None,
            max_queue=16,
            open_timeout=5,
            close_timeout=5,
            ping_interval=None,
        ) as websocket:
            output_lock = asyncio.Lock()

            async def write_stdout(message: str) -> None:
                async with output_lock:
                    writer.write(message.encode("utf-8") + b"\n")
                    await writer.drain()

            async def stdin_to_websocket() -> None:
                while line := await reader.readline():
                    frame = _frame_from_line(line)
                    if config.suppress_t3_mcp_reload:
                        response = _intercept_t3_mcp_reload(frame)
                        if response is not None:
                            await write_stdout(response)
                            continue
                    await websocket.send(frame)

            async def websocket_to_stdout() -> None:
                async for message in websocket:
                    if not isinstance(message, str):
                        raise ShimTransportError("shared App Server sent a binary frame")
                    await write_stdout(message)
                raise ShimTransportError("shared App Server connection closed")

            upstream = asyncio.create_task(stdin_to_websocket(), name="shared-shim-stdin")
            downstream = asyncio.create_task(websocket_to_stdout(), name="shared-shim-stdout")
            tasks = (upstream, downstream)
            try:
                done, _ = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if downstream in done:
                    downstream.result()
                if upstream in done:
                    upstream.result()
                    await websocket.close()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if owned_stdio:
            writer.close()
            with suppress(BrokenPipeError, ConnectionError, NotImplementedError):
                await writer.wait_closed()


def _safe_error_label(exc: BaseException) -> str:
    label = type(exc).__name__
    cause = getattr(exc, "__cause__", None)
    code = getattr(exc, "code", None) or getattr(cause, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return f"{label}:{code}"
    return label


def run_shim(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
) -> int:
    destination = stderr or sys.stderr
    try:
        config = parse_shim_args(argv, environ=environ)
        asyncio.run(relay_stdio_to_unix_websocket(config))
    except ShimUsageError as exc:
        destination.write(f"imcodex shared App Server shim: {exc}\n")
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary must return a safe fixed diagnostic.
        destination.write(
            "imcodex shared App Server shim: transport failed "
            f"({_safe_error_label(exc)})\n"
        )
        return 1
    return 0


def main() -> int:
    return run_shim(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from imcodex.appserver.shared_stdio_shim import (
    SharedAppServerShimConfig,
    ShimUsageError,
    _safe_error_label,
    parse_shim_args,
    relay_stdio_to_unix_websocket,
    run_shim,
)


def test_parse_defaults_to_native_control_socket(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    config = parse_shim_args(["app-server"], environ={"CODEX_HOME": str(codex_home)})

    assert config.endpoint == "unix://"
    assert config.socket_path == codex_home / "app-server-control" / "app-server-control.sock"


def test_parse_requires_explicit_chat_sync_mode_for_t3_mcp_args(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "shared.sock"

    arguments = [
        "app-server",
        "--connect",
        f"unix://{socket_path}",
        "-c",
        "mcp_servers.t3-code.url=http://127.0.0.1:3773/mcp",
        "--config=mcp_servers.t3-code.bearer_token_env_var=\"T3_MCP_BEARER_TOKEN\"",
    ]

    with pytest.raises(ShimUsageError, match="unsafe"):
        parse_shim_args(arguments, environ={})

    config = parse_shim_args(
        [*arguments[:3], "--chat-sync-without-t3-mcp", *arguments[3:]],
        environ={},
    )

    assert config.socket_path == socket_path
    assert config.suppress_t3_mcp_reload is True


@pytest.mark.parametrize(
    "extra_arguments",
    [
        ["--chat-sync-without-t3-mcp"],
        [
            "--chat-sync-without-t3-mcp",
            "-c",
            "mcp_servers.t3-code.url=http://127.0.0.1/mcp",
        ],
        [
            "--chat-sync-without-t3-mcp",
            "-c",
            "mcp_servers.t3-code.url=http://127.0.0.1/mcp",
            "-c",
            "mcp_servers.t3-code.url=http://127.0.0.1/duplicate",
            "-c",
            'mcp_servers.t3-code.bearer_token_env_var="T3_MCP_BEARER_TOKEN"',
        ],
        ["--chat-sync-without-t3-mcp", "-c", "model=unexpected"],
        [
            "--chat-sync-without-t3-mcp",
            "-c",
            "mcp_servers.t3-code.url=http://127.0.0.1/mcp",
            "-c",
            'mcp_servers.t3-code.bearer_token_env_var="OTHER_TOKEN"',
        ],
    ],
)
def test_parse_chat_sync_mode_fails_closed_for_incomplete_or_unknown_config(
    extra_arguments: list[str],
) -> None:
    with pytest.raises(ShimUsageError):
        parse_shim_args(["app-server", *extra_arguments], environ={})


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["app-server", "--listen", "stdio://"],
        ["app-server", "-c", "model=unexpected"],
        ["app-server", "--connect", "ws://127.0.0.1:8765"],
    ],
)
def test_parse_rejects_unsupported_launch_shapes(arguments: list[str]) -> None:
    with pytest.raises(ShimUsageError):
        parse_shim_args(arguments, environ={})


def test_usage_error_does_not_echo_unknown_argument() -> None:
    stderr = StringIO()

    result = run_shim(
        ["app-server", "--credential=must-not-echo"],
        environ={},
        stderr=stderr,
    )

    assert result == 2
    assert "must-not-echo" not in stderr.getvalue()

    stderr = StringIO()
    result = run_shim(
        [
            "app-server",
            "-c",
            "mcp_servers.t3-code.url=http://127.0.0.1/mcp?token=must-not-echo",
        ],
        environ={},
        stderr=stderr,
    )
    assert result == 2
    assert "must-not-echo" not in stderr.getvalue()


def test_safe_error_label_does_not_echo_string_code() -> None:
    error = RuntimeError("also-must-not-echo")
    error.code = "must-not-echo"  # type: ignore[attr-defined]

    assert _safe_error_label(error) == "RuntimeError"


def test_repository_launcher_uses_the_shim_module() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    launcher = repo_root / "scripts" / "imcodex-shared-app-server"
    script = launcher.read_text(encoding="utf-8")

    assert launcher.stat().st_mode & 0o111
    assert "imcodex.appserver.shared_stdio_shim" in script
    assert "IMCODEX_SHARED_APP_SERVER_PYTHON" in script
    assert "IMCODEX_SHARED_APP_SERVER_CODEX_BIN" in script


def test_repository_launcher_delegates_non_app_server_commands_to_codex() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(repo_root / "scripts" / "imcodex-shared-app-server"), "exec", "--version"],
        env={
            "PATH": "/usr/bin:/bin",
            "IMCODEX_SHARED_APP_SERVER_CODEX_BIN": "/bin/echo",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "exec --version"


def test_repository_launcher_rejects_recursive_codex_delegation() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    launcher = repo_root / "scripts" / "imcodex-shared-app-server"
    result = subprocess.run(
        [str(launcher), "exec", "--version"],
        env={
            "PATH": "/usr/bin:/bin",
            "IMCODEX_SHARED_APP_SERVER_CODEX_BIN": str(launcher),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert str(launcher) not in result.stderr


def test_repository_launcher_rejects_symlinked_recursive_codex_delegation(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    launcher = repo_root / "scripts" / "imcodex-shared-app-server"
    disguised_codex = tmp_path / "codex"
    disguised_codex.symlink_to(launcher)
    result = subprocess.run(
        [str(launcher), "exec", "--version"],
        env={
            "PATH": "/usr/bin:/bin",
            "IMCODEX_SHARED_APP_SERVER_CODEX_BIN": str(disguised_codex),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    assert str(disguised_codex) not in result.stderr


def test_repository_launcher_reports_missing_python_without_path_disclosure(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(repo_root / "scripts" / "imcodex-shared-app-server"), "app-server"],
        env={
            "PATH": "/usr/bin:/bin",
            "IMCODEX_SHARED_APP_SERVER_PYTHON": str(tmp_path / "secret-name"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "secret-name" not in result.stderr


async def test_relay_requires_reader_and_writer_as_a_pair(tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    config = SharedAppServerShimConfig("unix://", tmp_path / "unused.sock")

    with pytest.raises(ValueError, match="supplied together"):
        await relay_stdio_to_unix_websocket(config, reader=reader)


async def test_relay_cancellation_cleans_up_both_direction_tasks() -> None:
    connected = asyncio.Event()

    async def handler(websocket) -> None:
        connected.set()
        await websocket.wait_closed()

    class MemoryWriter:
        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            relay = asyncio.create_task(
                relay_stdio_to_unix_websocket(
                    SharedAppServerShimConfig("unix://", socket_path),
                    reader=asyncio.StreamReader(),
                    writer=MemoryWriter(),  # type: ignore[arg-type]
                )
            )
            await asyncio.wait_for(connected.wait(), 5)
            relay.cancel()
            with pytest.raises(asyncio.CancelledError):
                await relay
            await asyncio.sleep(0)

    orphan_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name() in {"shared-shim-stdin", "shared-shim-stdout"}
    }
    assert orphan_names == set()


async def _start_shim(
    socket_path: Path,
    *extra_arguments: str,
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "imcodex.appserver.shared_stdio_shim",
        "app-server",
        "--connect",
        f"unix://{socket_path}",
        *extra_arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2 * 1024 * 1024,
    )


async def test_subprocess_relays_jsonl_frames_and_closes_cleanly() -> None:
    received: list[str] = []

    async def handler(websocket) -> None:
        async for message in websocket:
            received.append(message)
            await websocket.send('{"id":1,"result":{"ok":true}}')

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(socket_path)
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b'{"id":1,"method":"thread/list"}\n')
            await process.stdin.drain()
            assert await asyncio.wait_for(process.stdout.readline(), 5) == (
                b'{"id":1,"result":{"ok":true}}\n'
            )
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0

    assert received == ['{"id":1,"method":"thread/list"}']


async def test_chat_sync_intercepts_t3_mcp_reload_without_forwarding_it() -> None:
    received: list[str] = []

    async def handler(websocket) -> None:
        async for message in websocket:
            received.append(message)
            await websocket.send('{"id":42,"result":{"thread":true}}')

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(
                socket_path,
                "--chat-sync-without-t3-mcp",
                "-c",
                "mcp_servers.t3-code.url=http://127.0.0.1:3773/mcp",
                "-c",
                'mcp_servers.t3-code.bearer_token_env_var="T3_MCP_BEARER_TOKEN"',
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b'{"id":41,"method":"config/mcpServer/reload"}\n')
            process.stdin.write(b'{"id":42,"method":"thread/read"}\n')
            await process.stdin.drain()
            responses = [
                await asyncio.wait_for(process.stdout.readline(), 5),
                await asyncio.wait_for(process.stdout.readline(), 5),
            ]
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0

    decoded = {message["id"]: message for line in responses if (message := json.loads(line))}
    assert decoded[41] == {"id": 41, "result": {}}
    assert decoded[42] == {"id": 42, "result": {"thread": True}}
    assert received == ['{"id":42,"method":"thread/read"}']


@pytest.mark.parametrize(
    "blocked_frame",
    [
        b'{"id":1,"method":"config/value/write","params":{"value":"must-not-echo"}}\n',
        b'{"id":1,"method":"account/login/start","params":{"token":"must-not-echo"}}\n',
        b'{"id":1,"method":"account/logout","params":{"token":"must-not-echo"}}\n',
        b'{"id":1,"method":"account/logout/start","params":{"token":"must-not-echo"}}\n',
    ],
)
async def test_chat_sync_rejects_other_global_mutations_without_forwarding_or_echoing(
    blocked_frame: bytes,
) -> None:
    received: list[str] = []

    async def handler(websocket) -> None:
        async for message in websocket:
            received.append(message)

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(
                socket_path,
                "--chat-sync-without-t3-mcp",
                "-c",
                "mcp_servers.t3-code.url=http://127.0.0.1:3773/mcp",
                "-c",
                'mcp_servers.t3-code.bearer_token_env_var="T3_MCP_BEARER_TOKEN"',
            )
            assert process.stdin is not None
            process.stdin.write(blocked_frame)
            await process.stdin.drain()
            assert await asyncio.wait_for(process.wait(), 5) == 1
            assert process.stderr is not None
            stderr = await process.stderr.read()

    assert received == []
    assert b"must-not-echo" not in stderr
    assert b"config/value/write" not in stderr
    assert b"account/" not in stderr


async def test_subprocess_applies_stdout_backpressure_to_large_text_frame() -> None:
    payload = '{"method":"probe","params":{"text":"' + ("x" * 1_000_000) + '"}}'

    async def handler(websocket) -> None:
        await websocket.recv()
        await websocket.send(payload)
        await websocket.wait_closed()

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(socket_path)
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b'{}\n')
            await process.stdin.drain()
            assert await asyncio.wait_for(process.stdout.readline(), 10) == payload.encode() + b"\n"
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_subprocess_rejects_binary_frame_without_echoing_payload() -> None:
    async def handler(websocket) -> None:
        await websocket.send(b"must-not-echo")

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(socket_path)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)

    assert process.returncode == 1
    assert stdout == b""
    assert b"must-not-echo" not in stderr
    assert b"ShimTransportError" in stderr


async def test_subprocess_fails_when_shared_server_closes_with_stdin_open() -> None:
    async def handler(websocket) -> None:
        await websocket.close()

    with tempfile.TemporaryDirectory(prefix="imcodex-shim-", dir="/tmp") as directory:
        socket_path = Path(directory) / "server.sock"
        async with unix_serve(handler, str(socket_path), compression=None):
            process = await _start_shim(socket_path)
            assert process.stdin is not None
            assert await asyncio.wait_for(process.wait(), 5) == 1
            process.stdin.close()
            assert process.stderr is not None
            assert b"ShimTransportError" in await process.stderr.read()

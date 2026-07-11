from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any, TextIO

from .config import load_codex_bin


ProcessRunner = Callable[..., subprocess.CompletedProcess[Any]]
Which = Callable[[str], str | None]
NATIVE_DAEMON_COMMANDS = {
    "start": "start",
    "restart": "restart",
    "stop": "stop",
    "status": "version",
}


def resolve_native_command(
    command: list[str],
    *,
    os_name: str | None = None,
    which: Which | None = None,
) -> list[str]:
    platform_name = os.name if os_name is None else os_name
    if platform_name != "nt" or not command:
        return command

    executable = command[0]
    if executable.lower().endswith(".cmd"):
        return ["cmd.exe", "/c", executable, *command[1:]]
    if any(separator in executable for separator in ("\\", "/")) or "." in executable:
        return command

    find_executable = which or shutil.which
    if shim := find_executable(f"{executable}.cmd"):
        return ["cmd.exe", "/c", shim, *command[1:]]
    if native_executable := find_executable(f"{executable}.exe"):
        return [native_executable, *command[1:]]
    return command


def run_app_server_cli(
    argv: list[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    codex_bin: str | None = None,
    process_runner: ProcessRunner | None = None,
    os_name: str | None = None,
    which: Which | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="imcodex app-server",
        description="Delegate App Server lifecycle operations to the native Codex daemon.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="Start the native App Server daemon if needed")
    subparsers.add_parser("restart", help="Restart the native App Server daemon")
    subparsers.add_parser("stop", help="Stop the native App Server daemon")
    subparsers.add_parser("status", help="Print native CLI and running App Server versions")
    args = parser.parse_args(argv)

    native_command = NATIVE_DAEMON_COMMANDS[args.command]
    executable = codex_bin or load_codex_bin()
    command = resolve_native_command(
        [executable, "app-server", "daemon", native_command],
        os_name=os_name,
        which=which,
    )
    runner = process_runner or subprocess.run
    try:
        completed = runner(
            command,
            check=False,
            stdout=subprocess.PIPE if stdout is not None else None,
            stderr=subprocess.PIPE if stderr is not None else None,
            text=True,
        )
    except FileNotFoundError as exc:
        destination = stderr or sys.stderr
        destination.write(f"imcodex: Codex executable was not found: {exc}\n")
        return 127
    except OSError as exc:
        destination = stderr or sys.stderr
        destination.write(f"imcodex: failed to execute the Codex daemon command: {exc}\n")
        return 126
    if stdout is not None and completed.stdout:
        stdout.write(str(completed.stdout))
    if stderr is not None and completed.stderr:
        stderr.write(str(completed.stderr))
    return int(completed.returncode)

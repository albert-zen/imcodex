from __future__ import annotations

import sys

import uvicorn

from .application import create_application
from .config import Settings
from .core_cli import run_core_cli
from .debug_harness.cli import run_debug_cli
from .ops_cli import run_ops_cli


def run(argv: list[str] | None = None) -> int | None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "debug":
        return run_debug_cli(argv[1:])
    if argv and argv[0] == "core":
        return run_core_cli(argv[1:])
    if argv and argv[0] == "ops":
        return run_ops_cli(argv[1:])
    settings = Settings.from_env()
    uvicorn.run(
        create_application(settings=settings),
        host=settings.http_host,
        port=settings.http_port,
    )
    return 0


if __name__ == "__main__":
    run()

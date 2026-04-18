from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core_manager import DedicatedCoreManager


def run_core_cli(argv: list[str], *, stdout=None, manager: DedicatedCoreManager | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="imcodex core")
    parser.add_argument("--root", default=str(Path.cwd() / ".imcodex-core"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--port", type=int, default=8765)

    subparsers.add_parser("stop")
    subparsers.add_parser("status")

    args = parser.parse_args(argv)
    manager = manager or DedicatedCoreManager(root=Path(args.root), repo_root=Path.cwd())

    if args.command == "start":
        manifest = manager.start(port=args.port)
    elif args.command == "stop":
        manifest = manager.stop()
    elif args.command == "status":
        manifest = manager.status()
    else:
        raise SystemExit(f"unsupported core command: {args.command}")

    stdout.write(json.dumps(manifest.to_dict(), ensure_ascii=True) + "\n")
    return 0

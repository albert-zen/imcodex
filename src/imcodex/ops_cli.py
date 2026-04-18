from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ops import BridgeRestartExecutor


def run_ops_cli(argv: list[str], *, stdout=None, executor: BridgeRestartExecutor | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="imcodex ops")
    subparsers = parser.add_subparsers(dest="command", required=True)

    restart = subparsers.add_parser("restart")
    restart.add_argument("--launch-snapshot", required=True)
    restart.add_argument("--timeout", type=float, default=30.0)

    args = parser.parse_args(argv)
    executor = executor or BridgeRestartExecutor()

    if args.command == "restart":
        result = executor.restart(Path(args.launch_snapshot), timeout_s=args.timeout)
    else:
        raise SystemExit(f"unsupported ops command: {args.command}")

    stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0

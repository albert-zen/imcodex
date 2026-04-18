from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .client import DebugHarnessClient
from .inspect import DebugHarnessInspector
from .manager import DebugInstanceManager
from . import scenarios as scenario_module


def run_debug_cli(
    argv: list[str],
    *,
    stdout=None,
    manager: DebugInstanceManager | None = None,
    client: DebugHarnessClient | None = None,
    inspector: DebugHarnessInspector | None = None,
    scenarios=None,
) -> int:
    stdout = stdout or sys.stdout
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.lab_root)
    manager = manager or DebugInstanceManager(root=root, repo_root=Path.cwd())
    client = client or DebugHarnessClient()
    inspector = inspector or DebugHarnessInspector()
    scenarios = scenarios or scenario_module

    if args.command == "start":
        manifest = manager.start(port=args.port, purpose=args.purpose, qq_enabled=args.qq, app_server_url=args.app_server_url)
        result: dict[str, Any] = {"manifest": manifest.to_dict()}
        if args.wait:
            result["health"] = manager.wait_until_healthy(manifest.run_id, timeout_s=args.timeout)
        _write(stdout, result)
        return 0
    if args.command == "stop":
        manifest = manager.stop(args.run_id)
        _write(stdout, manifest.to_dict())
        return 0
    if args.command == "runs":
        _write(stdout, {"runs": [manifest.to_dict() for manifest in manager.list_runs()]})
        return 0
    if args.command == "send":
        manifest = manager.get_run(args.run_id)
        response = client.send(
            manifest=manifest,
            channel_id=args.channel_id,
            conversation_id=args.conversation,
            user_id=args.user_id,
            text=args.text,
            thread_id=args.thread,
        )
        _write(stdout, response)
        return 0
    if args.command == "inspect":
        manifest = manager.get_run(args.run_id)
        payload: dict[str, Any] = {"run": inspector.inspect_run(manifest, tail=args.tail)}
        if args.conversation:
            payload["conversation"] = inspector.inspect_conversation(manifest, args.channel_id, args.conversation)
        if args.thread:
            payload["thread"] = inspector.inspect_thread(manifest, args.thread)
        if args.live:
            payload["runtime"] = inspector.inspect_runtime_state(manifest)
        _write(stdout, payload)
        return 0
    if args.command == "events":
        manifest = manager.get_run(args.run_id)
        _write(stdout, {"events": inspector.tail_events(manifest, tail=args.tail, prefix=args.filter_prefix)})
        return 0
    if args.command == "scenario":
        if args.scenario_name == "restart-gap":
            result = scenarios.run_restart_gap_scenario(
                manager=manager,
                port=args.port,
            )
            _write(stdout, result)
            return 0
        if args.scenario_name == "approval-stall":
            result = scenarios.run_approval_stall_scenario(
                manager=manager,
                client=client,
                inspector=inspector,
                port=args.port,
            )
            _write(stdout, result)
            return 0
        raise SystemExit(f"unsupported scenario: {args.scenario_name}")
    raise SystemExit(f"unsupported command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imcodex debug")
    parser.add_argument("--lab-root", default=str(Path.cwd().parent / "imcodex-debug-lab"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--port", type=int, required=True)
    start.add_argument("--purpose")
    start.add_argument("--qq", action="store_true")
    start.add_argument("--app-server-url")
    start.add_argument("--wait", action="store_true")
    start.add_argument("--timeout", type=float, default=30.0)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--run-id", required=True)

    subparsers.add_parser("runs")

    send = subparsers.add_parser("send")
    send.add_argument("--run-id", required=True)
    send.add_argument("--conversation", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--thread")
    send.add_argument("--channel-id", default="debug")
    send.add_argument("--user-id", default="debug-user")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--conversation")
    inspect.add_argument("--thread")
    inspect.add_argument("--channel-id", default="debug")
    inspect.add_argument("--tail", type=int, default=20)
    inspect.add_argument("--live", action="store_true")

    events = subparsers.add_parser("events")
    events.add_argument("--run-id", required=True)
    events.add_argument("--tail", type=int, default=20)
    events.add_argument("--filter-prefix")

    scenario = subparsers.add_parser("scenario")
    scenario.add_argument("scenario_name", choices=["restart-gap", "approval-stall"])
    scenario.add_argument("--port", type=int, required=True)
    return parser


def _write(stdout, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")

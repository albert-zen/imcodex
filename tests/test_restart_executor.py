from __future__ import annotations

import json
from pathlib import Path

from imcodex.ops import BridgeRestartExecutor


def test_restart_executor_reads_launch_snapshot_and_restarts_bridge(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    def starter(*, command: list[str], cwd: Path, env: dict[str, str]):
        calls.append(("start", {"command": command, "cwd": cwd, "env": env}))

        class _Process:
            pid = 65432

        return _Process()

    def stopper(pid: int) -> None:
        calls.append(("stop", pid))

    def waiter(port: int, timeout_s: float) -> dict[str, object]:
        calls.append(("wait", {"port": port, "timeout_s": timeout_s}))
        return {"status": "healthy", "port": port}

    launch_snapshot = {
        "command": ["python", "-m", "imcodex"],
        "cwd": r"D:\desktop\imcodex",
        "env": {
            "IMCODEX_HTTP_PORT": "8000",
            "IMCODEX_CORE_MODE": "dedicated-ws",
            "IMCODEX_CORE_URL": "ws://127.0.0.1:8765",
        },
        "pid": 44584,
        "port": 8000,
    }
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(json.dumps(launch_snapshot), encoding="utf-8")

    executor = BridgeRestartExecutor(
        launcher=starter,
        stopper=stopper,
        health_waiter=waiter,
    )

    result = executor.restart(launch_path, timeout_s=15.0)

    assert result["health"]["status"] == "healthy"
    assert calls[0] == ("stop", 44584)
    assert calls[1][0] == "start"
    assert calls[1][1]["command"] == ["python", "-m", "imcodex"]
    assert calls[2] == ("wait", {"port": 8000, "timeout_s": 15.0})

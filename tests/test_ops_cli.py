from __future__ import annotations

import io
import json
from pathlib import Path

from imcodex.ops_cli import run_ops_cli


class _StubExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, float]] = []

    def restart(self, launch_snapshot_path: Path, *, timeout_s: float = 30.0) -> dict[str, object]:
        self.calls.append((launch_snapshot_path, timeout_s))
        return {"pid": 65432, "port": 8000, "health": {"status": "healthy"}}


def test_ops_cli_restart_uses_launch_snapshot_path() -> None:
    output = io.StringIO()
    executor = _StubExecutor()

    exit_code = run_ops_cli(
        ["restart", "--launch-snapshot", r"D:\desktop\imcodex\.imcodex-run\current\launch.json", "--timeout", "15"],
        stdout=output,
        executor=executor,
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert executor.calls == [(Path(r"D:\desktop\imcodex\.imcodex-run\current\launch.json"), 15.0)]
    assert body["health"]["status"] == "healthy"

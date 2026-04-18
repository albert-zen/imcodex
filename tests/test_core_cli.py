from __future__ import annotations

import io
import json
from pathlib import Path

from imcodex.core_cli import run_core_cli
from imcodex.core_manager import DedicatedCoreManifest


class _StubCoreManager:
    def __init__(self, manifest: DedicatedCoreManifest) -> None:
        self.manifest = manifest
        self.started: list[int] = []
        self.stopped = False

    def start(self, *, port: int) -> DedicatedCoreManifest:
        self.started.append(port)
        return self.manifest

    def stop(self) -> DedicatedCoreManifest:
        self.stopped = True
        return self.manifest

    def status(self) -> DedicatedCoreManifest:
        return self.manifest


def _manifest() -> DedicatedCoreManifest:
    return DedicatedCoreManifest(
        pid=42001,
        port=8765,
        url="ws://127.0.0.1:8765",
        started_at="2026-04-19T12:00:01+08:00",
        status="running",
    )


def test_core_cli_start_writes_manifest_json() -> None:
    output = io.StringIO()
    manager = _StubCoreManager(_manifest())

    exit_code = run_core_cli(["start", "--port", "8765"], stdout=output, manager=manager)

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert manager.started == [8765]
    assert body["url"] == "ws://127.0.0.1:8765"


def test_core_cli_stop_and_status() -> None:
    output = io.StringIO()
    manager = _StubCoreManager(_manifest())

    stop_code = run_core_cli(["stop"], stdout=output, manager=manager)
    status_code = run_core_cli(["status"], stdout=output, manager=manager)

    lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    assert stop_code == 0
    assert status_code == 0
    assert manager.stopped is True
    assert lines[-1]["pid"] == 42001

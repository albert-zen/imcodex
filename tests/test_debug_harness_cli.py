from __future__ import annotations

import io
import json
from pathlib import Path

from imcodex.debug_harness.cli import run_debug_cli
from imcodex.debug_harness.models import DebugRunManifest


class _StubManager:
    def __init__(self, manifest: DebugRunManifest) -> None:
        self.manifest = manifest
        self.started: list[dict] = []
        self.stopped: list[str] = []

    def start(self, *, port: int, purpose: str | None, qq_enabled: bool, app_server_url: str | None):
        self.started.append(
            {
                "port": port,
                "purpose": purpose,
                "qq_enabled": qq_enabled,
                "app_server_url": app_server_url,
            }
        )
        return self.manifest

    def wait_until_healthy(self, run_id: str, *, timeout_s: float = 30.0):
        return {"run_id": run_id, "status": "healthy", "timeout_s": timeout_s}

    def stop(self, run_id: str):
        self.stopped.append(run_id)
        return self.manifest

    def list_runs(self):
        return [self.manifest]

    def get_run(self, run_id: str):
        assert run_id == self.manifest.run_id
        return self.manifest


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, **payload):
        self.calls.append(payload)
        return {"messages": [{"text": payload["text"]}]}


class _StubInspector:
    def inspect_run(self, manifest, *, tail: int = 20):
        return {"manifest": manifest.to_dict(), "tail": tail}

    def inspect_runtime_state(self, manifest):
        return {"run_id": manifest.run_id, "mode": "shared-ws"}

    def inspect_conversation(self, manifest, channel_id: str, conversation_id: str):
        return {"run_id": manifest.run_id, "channel_id": channel_id, "conversation_id": conversation_id}

    def inspect_thread(self, manifest, thread_id: str):
        return {"run_id": manifest.run_id, "thread_id": thread_id}

    def tail_events(self, manifest, *, tail: int = 20, prefix: str | None = None):
        return [{"run_id": manifest.run_id, "tail": tail, "prefix": prefix}]


class _StubScenarios:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def run_restart_gap_scenario(self, **payload):
        self.calls.append(("restart-gap", payload))
        return {"scenario": "restart-gap", "result": "ok"}

    def run_approval_stall_scenario(self, **payload):
        self.calls.append(("approval-stall", payload))
        return {"scenario": "approval-stall", "result": "ok"}

    def run_approval_live_scenario(self, **payload):
        self.calls.append(("approval-live", payload))
        return {"scenario": "approval-live", "result": "ok"}


def _manifest() -> DebugRunManifest:
    return DebugRunManifest(
        run_id="debug-1",
        pid=51234,
        port=8011,
        purpose="test",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-1")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-1")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-1")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )


def test_cli_start_prints_manifest_and_health() -> None:
    output = io.StringIO()
    manifest = _manifest()
    manager = _StubManager(manifest)

    exit_code = run_debug_cli(
        ["--lab-root", r"D:\desktop\imcodex-debug-lab", "start", "--port", "8011", "--wait"],
        stdout=output,
        manager=manager,
        client=_StubClient(),
        inspector=_StubInspector(),
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert body["manifest"]["run_id"] == "debug-1"
    assert body["health"]["status"] == "healthy"


def test_cli_send_uses_thread_target_when_requested() -> None:
    output = io.StringIO()
    manifest = _manifest()
    manager = _StubManager(manifest)
    client = _StubClient()

    exit_code = run_debug_cli(
        [
            "--lab-root",
            r"D:\desktop\imcodex-debug-lab",
            "send",
            "--run-id",
            "debug-1",
            "--conversation",
            "conv-1",
            "--thread",
            "thr-1",
            "--text",
            "hello",
        ],
        stdout=output,
        manager=manager,
        client=client,
        inspector=_StubInspector(),
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert body["messages"][0]["text"] == "hello"
    assert client.calls[0]["thread_id"] == "thr-1"


def test_cli_scenario_restart_gap_runs_named_scenario() -> None:
    output = io.StringIO()
    manifest = _manifest()
    manager = _StubManager(manifest)
    scenarios = _StubScenarios()

    exit_code = run_debug_cli(
        [
            "--lab-root",
            r"D:\desktop\imcodex-debug-lab",
            "scenario",
            "restart-gap",
            "--port",
            "8014",
        ],
        stdout=output,
        manager=manager,
        client=_StubClient(),
        inspector=_StubInspector(),
        scenarios=scenarios,
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert body["scenario"] == "restart-gap"
    assert scenarios.calls[0][0] == "restart-gap"
    assert scenarios.calls[0][1]["port"] == 8014


def test_cli_scenario_approval_stall_wires_dependencies() -> None:
    output = io.StringIO()
    manifest = _manifest()
    manager = _StubManager(manifest)
    client = _StubClient()
    inspector = _StubInspector()
    scenarios = _StubScenarios()

    exit_code = run_debug_cli(
        [
            "--lab-root",
            r"D:\desktop\imcodex-debug-lab",
            "scenario",
            "approval-stall",
            "--port",
            "8015",
        ],
        stdout=output,
        manager=manager,
        client=client,
        inspector=inspector,
        scenarios=scenarios,
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert body["scenario"] == "approval-stall"
    assert scenarios.calls[0][0] == "approval-stall"
    assert scenarios.calls[0][1]["manager"] is manager
    assert scenarios.calls[0][1]["client"] is client
    assert scenarios.calls[0][1]["inspector"] is inspector
    assert scenarios.calls[0][1]["port"] == 8015


def test_cli_scenario_approval_live_wires_dependencies() -> None:
    output = io.StringIO()
    manifest = _manifest()
    manager = _StubManager(manifest)
    client = _StubClient()
    inspector = _StubInspector()
    scenarios = _StubScenarios()

    exit_code = run_debug_cli(
        [
            "--lab-root",
            r"D:\desktop\imcodex-debug-lab",
            "scenario",
            "approval-live",
            "--port",
            "8016",
        ],
        stdout=output,
        manager=manager,
        client=client,
        inspector=inspector,
        scenarios=scenarios,
    )

    body = json.loads(output.getvalue())
    assert exit_code == 0
    assert body["scenario"] == "approval-live"
    assert scenarios.calls[0][0] == "approval-live"
    assert scenarios.calls[0][1]["manager"] is manager
    assert scenarios.calls[0][1]["client"] is client
    assert scenarios.calls[0][1]["inspector"] is inspector
    assert scenarios.calls[0][1]["port"] == 8016

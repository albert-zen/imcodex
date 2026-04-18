from __future__ import annotations

from pathlib import Path

from imcodex.debug_harness.models import DebugRunManifest
from imcodex.debug_harness.scenarios import (
    run_approval_live_scenario,
    run_approval_resume_live_scenario,
    run_bridge_restart_live_scenario,
    run_approval_stall_scenario,
    run_restart_gap_scenario,
)
from imcodex.core_manager import DedicatedCoreManifest


class _StubManager:
    def __init__(self, manifest: DebugRunManifest) -> None:
        self.manifest = manifest
        self.started = False
        self.start_calls: list[dict[str, object]] = []
        self.stopped = False
        self.running = True

    def start(
        self,
        *,
        port: int,
        purpose: str | None,
        qq_enabled: bool,
        app_server_url: str | None,
        core_mode: str | None = None,
        core_url: str | None = None,
    ):
        assert port == self.manifest.port
        assert purpose == self.manifest.purpose
        assert qq_enabled is False
        assert app_server_url is None
        self.start_calls.append({"core_mode": core_mode, "core_url": core_url})
        self.started = True
        return self.manifest

    def wait_until_healthy(self, run_id: str, *, timeout_s: float = 30.0):
        assert run_id == self.manifest.run_id
        return {"status": "healthy", "timeout_s": timeout_s}

    def stop(self, run_id: str):
        assert run_id == self.manifest.run_id
        self.stopped = True
        self.running = False
        return self.manifest

    def is_port_listening(self, port: int) -> bool:
        assert port == self.manifest.port
        return self.running


class _StubCoreManager:
    def __init__(self, manifest: DedicatedCoreManifest) -> None:
        self.manifest = manifest
        self.started: list[int] = []
        self.stopped = False

    def start(self, *, port: int) -> DedicatedCoreManifest:
        self.started.append(port)
        return self.manifest

    def wait_until_ready(self, *, port: int, timeout_s: float = 30.0) -> None:
        assert port == self.manifest.port
        assert timeout_s > 0

    def stop(self) -> DedicatedCoreManifest:
        self.stopped = True
        return self.manifest


class _StubExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def restart(self, launch_snapshot_path, *, timeout_s: float = 30.0):
        self.calls.append({"launch_snapshot_path": launch_snapshot_path, "timeout_s": timeout_s})
        return {"pid": 60001, "port": 8017, "health": {"status": "healthy"}}


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def inject_active_turn(self, **payload):
        self.calls.append(("active_turn", payload))
        return {"ok": True}

    def inject_server_request(self, **payload):
        self.calls.append(("server_request", payload))
        return {"ok": True}

    def force_client_reset(self, **payload):
        self.calls.append(("force_client_reset", payload))
        return {"ok": True}

    def send(self, **payload):
        self.calls.append(("send", payload))
        text = payload.get("text")
        if text == "/approve native-request-abcdef":
            response_text = "[System] Unknown approval request."
        else:
            response_text = text if isinstance(text, str) else ""
        return {
            "messages": [
                {
                    "text": response_text
                }
            ]
        }


class _StubInspector:
    def __init__(self) -> None:
        self.conversation_calls = 0
        self.runtime_calls = 0

    def inspect_conversation(self, manifest, channel_id: str, conversation_id: str):
        self.conversation_calls += 1
        if self.conversation_calls == 1:
            return {
                "binding": {"thread_id": "thr-real"},
                "active_turn": None,
                "pending_requests": [],
            }
        if self.conversation_calls == 2:
            return {
                "binding": {"thread_id": "thr-real"},
                "active_turn": {"turn_id": "turn-debug", "status": "inProgress"},
                "pending_requests": [{"request_id": "native-request-abcdef"}],
            }
        if self.conversation_calls == 3:
            return {
                "binding": {"thread_id": "thr-real"},
                "active_turn": None,
                "pending_requests": [],
            }
        return {
            "binding": {"thread_id": "thr-real"},
            "active_turn": {"turn_id": "turn-debug", "status": "inProgress"},
            "pending_requests": [],
        }

    def inspect_runtime_state(self, manifest):
        self.runtime_calls += 1
        return {"appserver": {"pending_server_request_ids": []}}

    def wait_for_pending_requests(self, manifest, channel_id: str, conversation_id: str, *, timeout_s: float, interval_s: float):
        return {
            "binding": {"thread_id": "thr-real"},
            "active_turn": {"turn_id": "turn-real", "status": "inProgress"},
            "pending_requests": [{"request_id": "native-request-live"}],
        }

    def wait_until_no_pending_requests(self, manifest, channel_id: str, conversation_id: str, *, timeout_s: float, interval_s: float):
        return {
            "binding": {"thread_id": "thr-real"},
            "active_turn": None,
            "pending_requests": [],
        }

    def wait_for_active_turn(self, manifest, channel_id: str, conversation_id: str, *, timeout_s: float, interval_s: float):
        return {
            "binding": {"thread_id": "thr-real"},
            "active_turn": {"turn_id": "turn-debug", "status": "inProgress"},
            "pending_requests": [],
        }


def test_run_approval_stall_scenario_captures_client_reset_desync() -> None:
    manifest = DebugRunManifest(
        run_id="debug-approval",
        pid=51234,
        port=8013,
        purpose="approval-stall",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-approval")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-approval")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-approval")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )
    manager = _StubManager(manifest)
    client = _StubClient()
    inspector = _StubInspector()

    result = run_approval_stall_scenario(manager=manager, client=client, inspector=inspector, port=8013)

    assert manager.started is True
    assert manager.stopped is True
    assert (
        result["response"]["messages"][0]["text"]
        == "[System] Unknown approval request."
    )
    assert result["before"]["pending_requests"][0]["request_id"] == "native-request-abcdef"
    assert result["runtime_before_reset"]["appserver"]["pending_server_request_ids"] == []
    assert result["runtime_after_reset"]["appserver"]["pending_server_request_ids"] == []
    assert result["after"]["pending_requests"] == []
    assert result["after"]["active_turn"] is None
    assert [call[0] for call in client.calls] == [
        "send",
        "send",
        "active_turn",
        "server_request",
        "force_client_reset",
        "send",
    ]


def test_run_restart_gap_scenario_reports_no_auto_restart_after_stop() -> None:
    manifest = DebugRunManifest(
        run_id="debug-restart",
        pid=51234,
        port=8014,
        purpose="restart-gap",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-restart")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-restart")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-restart")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )
    manager = _StubManager(manifest)

    result = run_restart_gap_scenario(manager=manager, port=8014)

    assert result["before_stop"]["status"] == "healthy"
    assert result["after_stop"]["port_listening"] is False
    assert result["after_stop"]["auto_restarted"] is False


def test_run_approval_live_scenario_waits_for_real_pending_requests() -> None:
    manifest = DebugRunManifest(
        run_id="debug-approval-live",
        pid=51234,
        port=8016,
        purpose="approval-live",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-approval-live")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-approval-live")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-approval-live")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )
    manager = _StubManager(manifest)
    client = _StubClient()
    inspector = _StubInspector()

    result = run_approval_live_scenario(manager=manager, client=client, inspector=inspector, port=8016)

    assert manager.started is True
    assert manager.stopped is True
    assert result["pending"]["pending_requests"][0]["request_id"] == "native-request-live"
    assert result["after"]["pending_requests"] == []
    assert result["after_followup"]["active_turn"]["turn_id"] == "turn-debug"
    assert [call[0] for call in client.calls] == [
        "send",
        "send",
        "send",
        "force_client_reset",
        "send",
        "send",
    ]
    assert client.calls[2][1]["text"].startswith("Run the PowerShell command")


def test_run_bridge_restart_live_scenario_uses_dedicated_core_and_restart_executor() -> None:
    manifest = DebugRunManifest(
        run_id="debug-bridge-restart",
        pid=51234,
        port=8017,
        purpose="bridge-restart-live",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-bridge-restart")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-bridge-restart")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-bridge-restart")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )
    core_manifest = DedicatedCoreManifest(
        pid=42001,
        port=8765,
        url="ws://127.0.0.1:8765",
        started_at="2026-04-19T10:29:00+08:00",
        status="running",
    )
    manager = _StubManager(manifest)
    core_manager = _StubCoreManager(core_manifest)
    client = _StubClient()
    inspector = _StubInspector()
    executor = _StubExecutor()

    result = run_bridge_restart_live_scenario(
        manager=manager,
        core_manager=core_manager,
        executor=executor,
        client=client,
        inspector=inspector,
        bridge_port=8017,
        core_port=8765,
    )

    assert core_manager.started == [8765]
    assert manager.start_calls[0]["core_mode"] == "dedicated-ws"
    assert manager.start_calls[0]["core_url"] == "ws://127.0.0.1:8765"
    assert executor.calls
    assert result["restart"]["health"]["status"] == "healthy"
    assert result["after_restart"]["active_turn"]["turn_id"] == "turn-debug"


def test_run_approval_resume_live_scenario_restarts_bridge_and_then_approves() -> None:
    manifest = DebugRunManifest(
        run_id="debug-approval-resume",
        pid=51234,
        port=8018,
        purpose="approval-resume-live",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-approval-resume")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-approval-resume")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-approval-resume")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )
    core_manifest = DedicatedCoreManifest(
        pid=42001,
        port=8765,
        url="ws://127.0.0.1:8765",
        started_at="2026-04-19T10:29:00+08:00",
        status="running",
    )
    manager = _StubManager(manifest)
    core_manager = _StubCoreManager(core_manifest)
    client = _StubClient()
    inspector = _StubInspector()
    executor = _StubExecutor()

    result = run_approval_resume_live_scenario(
        manager=manager,
        core_manager=core_manager,
        executor=executor,
        client=client,
        inspector=inspector,
        bridge_port=8018,
        core_port=8765,
    )

    assert core_manager.started == [8765]
    assert manager.start_calls[0]["core_mode"] == "dedicated-ws"
    assert result["pending"]["pending_requests"][0]["request_id"] == "native-request-live"
    assert result["restart"]["health"]["status"] == "healthy"
    assert result["approve_response"]["messages"][0]["text"] == "/approve"

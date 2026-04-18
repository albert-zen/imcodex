from __future__ import annotations

from pathlib import Path

from imcodex.debug_harness.models import DebugRunManifest
from imcodex.debug_harness.scenarios import run_approval_stall_scenario, run_restart_gap_scenario


class _StubManager:
    def __init__(self, manifest: DebugRunManifest) -> None:
        self.manifest = manifest
        self.started = False
        self.stopped = False
        self.running = True

    def start(self, *, port: int, purpose: str | None, qq_enabled: bool, app_server_url: str | None):
        assert port == self.manifest.port
        assert purpose == self.manifest.purpose
        assert qq_enabled is False
        assert app_server_url is None
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


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def inject_binding(self, **payload):
        self.calls.append(("binding", payload))
        return {"ok": True}

    def inject_active_turn(self, **payload):
        self.calls.append(("active_turn", payload))
        return {"ok": True}

    def inject_pending_request(self, **payload):
        self.calls.append(("pending_request", payload))
        return {"ok": True}

    def inject_client_pending_request(self, **payload):
        self.calls.append(("client_pending_request", payload))
        return {"ok": True}

    def force_client_reset(self, **payload):
        self.calls.append(("force_client_reset", payload))
        return {"ok": True}

    def send(self, **payload):
        self.calls.append(("send", payload))
        return {
            "messages": [
                {
                    "text": "[System] Request native-request-abcdef is out of sync with Codex and the active turn could not be stopped automatically. Try /stop."
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
                "binding": {"thread_id": "thr-debug"},
                "active_turn": {"turn_id": "turn-debug", "status": "inProgress"},
                "pending_requests": [{"request_id": "native-request-abcdef"}],
            }
        return {
            "binding": {"thread_id": "thr-debug"},
            "active_turn": {"turn_id": "turn-debug", "status": "inProgress"},
            "pending_requests": [{"request_id": "native-request-abcdef"}],
        }

    def inspect_runtime_state(self, manifest):
        self.runtime_calls += 1
        if self.runtime_calls == 1:
            return {"appserver": {"pending_server_request_ids": ["native-request-abcdef"]}}
        return {"appserver": {"pending_server_request_ids": []}}


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
        == "[System] Request native-request-abcdef is out of sync with Codex and the active turn could not be stopped automatically. Try /stop."
    )
    assert result["before"]["pending_requests"][0]["request_id"] == "native-request-abcdef"
    assert result["runtime_before_reset"]["appserver"]["pending_server_request_ids"] == ["native-request-abcdef"]
    assert result["runtime_after_reset"]["appserver"]["pending_server_request_ids"] == []
    assert result["after"]["pending_requests"][0]["request_id"] == "native-request-abcdef"
    assert result["after"]["active_turn"]["turn_id"] == "turn-debug"
    assert [call[0] for call in client.calls] == [
        "binding",
        "active_turn",
        "pending_request",
        "client_pending_request",
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

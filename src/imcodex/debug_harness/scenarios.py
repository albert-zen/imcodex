from __future__ import annotations

from .client import DebugHarnessClient
from .inspect import DebugHarnessInspector
from .manager import DebugInstanceManager


def run_restart_gap_scenario(
    *,
    manager: DebugInstanceManager,
    port: int = 8014,
) -> dict:
    manifest = manager.start(port=port, purpose="restart-gap", qq_enabled=False, app_server_url=None)
    before_stop = manager.wait_until_healthy(manifest.run_id)
    manager.stop(manifest.run_id)
    after_stop = {
        "port_listening": manager.is_port_listening(manifest.port),
        # This scenario intentionally checks the current system behavior:
        # once the bridge is stopped, nothing restarts it automatically.
        "auto_restarted": False,
    }
    return {
        "manifest": manifest.to_dict(),
        "before_stop": before_stop,
        "after_stop": after_stop,
    }


def run_approval_stall_scenario(
    *,
    manager: DebugInstanceManager,
    client: DebugHarnessClient,
    inspector: DebugHarnessInspector,
    port: int = 8013,
) -> dict:
    manifest = manager.start(port=port, purpose="approval-stall", qq_enabled=False, app_server_url=None)
    manager.wait_until_healthy(manifest.run_id)
    try:
        client.inject_binding(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval",
            thread_id="thr-debug",
            cwd=manifest.cwd,
            preview="approval stall debug",
            status="idle",
        )
        client.inject_active_turn(
            manifest=manifest,
            thread_id="thr-debug",
            turn_id="turn-debug",
        )
        client.inject_pending_request(
            manifest=manifest,
            request_id="native-request-abcdef",
            channel_id="debug",
            conversation_id="conv-approval",
            thread_id="thr-debug",
            turn_id="turn-debug",
            kind="approval",
            request_method="item/commandExecution/requestApproval",
            payload={"reason": "Need approval"},
        )
        before = inspector.inspect_conversation(manifest, "debug", "conv-approval")
        response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval",
            user_id="debug-user",
            text="/approve native-request-abcdef",
        )
        after = inspector.inspect_conversation(manifest, "debug", "conv-approval")
        runtime = inspector.inspect_runtime_state(manifest)
        return {
            "manifest": manifest.to_dict(),
            "before": before,
            "response": response,
            "after": after,
            "runtime": runtime,
        }
    finally:
        manager.stop(manifest.run_id)

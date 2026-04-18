from __future__ import annotations

from pathlib import Path

from ..core_manager import DedicatedCoreManager
from ..ops import BridgeRestartExecutor
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
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval",
            user_id="debug-user",
            text=f"/cwd {manifest.cwd}",
        )
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval",
            user_id="debug-user",
            text="/new",
        )
        bootstrap = inspector.inspect_conversation(manifest, "debug", "conv-approval")
        thread_id = str(bootstrap["binding"]["thread_id"] or "")
        client.inject_active_turn(
            manifest=manifest,
            thread_id=thread_id,
            turn_id="turn-debug",
        )
        client.inject_server_request(
            manifest=manifest,
            jsonrpc_id=99,
            method="item/commandExecution/requestApproval",
            request_id="native-request-abcdef",
            thread_id=thread_id,
            turn_id="turn-debug",
            payload={"reason": "Need approval"},
        )
        before = inspector.inspect_conversation(manifest, "debug", "conv-approval")
        runtime_before_reset = inspector.inspect_runtime_state(manifest)
        client.force_client_reset(manifest=manifest)
        runtime_after_reset = inspector.inspect_runtime_state(manifest)
        response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval",
            user_id="debug-user",
            text="/approve native-request-abcdef",
        )
        after = inspector.inspect_conversation(manifest, "debug", "conv-approval")
        return {
            "manifest": manifest.to_dict(),
            "before": before,
            "runtime_before_reset": runtime_before_reset,
            "runtime_after_reset": runtime_after_reset,
            "response": response,
            "after": after,
        }
    finally:
        manager.stop(manifest.run_id)


def run_approval_live_scenario(
    *,
    manager: DebugInstanceManager,
    client: DebugHarnessClient,
    inspector: DebugHarnessInspector,
    port: int = 8016,
) -> dict:
    manifest = manager.start(port=port, purpose="approval-live", qq_enabled=False, app_server_url=None)
    manager.wait_until_healthy(manifest.run_id)
    try:
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-live",
            user_id="debug-user",
            text=f"/cwd {manifest.cwd}",
        )
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-live",
            user_id="debug-user",
            text="/new",
        )
        prompt = (
            "Run the PowerShell command `Get-Date` in the current workspace and tell me the result. "
            "Use the available tools if needed."
        )
        initial_response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-live",
            user_id="debug-user",
            text=prompt,
        )
        pending = inspector.wait_for_pending_requests(
            manifest,
            "debug",
            "conv-approval-live",
            timeout_s=60.0,
            interval_s=0.5,
        )
        runtime_before_reset = inspector.inspect_runtime_state(manifest)
        client.force_client_reset(manifest=manifest)
        runtime_after_reset = inspector.inspect_runtime_state(manifest)
        approve_response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-live",
            user_id="debug-user",
            text="/approve",
        )
        after = inspector.wait_until_no_pending_requests(
            manifest,
            "debug",
            "conv-approval-live",
            timeout_s=30.0,
            interval_s=0.5,
        )
        followup_response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-live",
            user_id="debug-user",
            text="hello after reset",
        )
        after_followup = inspector.wait_for_active_turn(
            manifest,
            "debug",
            "conv-approval-live",
            timeout_s=30.0,
            interval_s=0.5,
        )
        return {
            "manifest": manifest.to_dict(),
            "initial_response": initial_response,
            "pending": pending,
            "runtime_before_reset": runtime_before_reset,
            "runtime_after_reset": runtime_after_reset,
            "approve_response": approve_response,
            "after": after,
            "followup_response": followup_response,
            "after_followup": after_followup,
        }
    finally:
        manager.stop(manifest.run_id)


def run_bridge_restart_live_scenario(
    *,
    manager: DebugInstanceManager,
    core_manager: DedicatedCoreManager,
    executor: BridgeRestartExecutor,
    client: DebugHarnessClient,
    inspector: DebugHarnessInspector,
    bridge_port: int = 8017,
    core_port: int = 8765,
) -> dict:
    core_manifest = core_manager.start(port=core_port)
    core_manager.wait_until_ready(port=core_port)
    manifest = manager.start(
        port=bridge_port,
        purpose="bridge-restart-live",
        qq_enabled=False,
        app_server_url=None,
        core_mode="dedicated-ws",
        core_url=core_manifest.url,
    )
    manager.wait_until_healthy(manifest.run_id)
    try:
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-bridge-restart",
            user_id="debug-user",
            text=f"/cwd {manifest.cwd}",
        )
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-bridge-restart",
            user_id="debug-user",
            text="/new",
        )
        before_restart = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-bridge-restart",
            user_id="debug-user",
            text="hello before restart",
        )
        launch_snapshot_path = Path(manifest.run_dir) / "current" / "launch.json"
        restart = executor.restart(launch_snapshot_path, timeout_s=30.0)
        after_restart_send = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-bridge-restart",
            user_id="debug-user",
            text="hello after restart",
        )
        after_restart = inspector.wait_for_active_turn(
            manifest,
            "debug",
            "conv-bridge-restart",
            timeout_s=30.0,
            interval_s=0.5,
        )
        return {
            "core": core_manifest.to_dict(),
            "manifest": manifest.to_dict(),
            "before_restart": before_restart,
            "restart": restart,
            "after_restart_send": after_restart_send,
            "after_restart": after_restart,
        }
    finally:
        manager.stop(manifest.run_id)
        core_manager.stop()


def run_approval_resume_live_scenario(
    *,
    manager: DebugInstanceManager,
    core_manager: DedicatedCoreManager,
    executor: BridgeRestartExecutor,
    client: DebugHarnessClient,
    inspector: DebugHarnessInspector,
    bridge_port: int = 8018,
    core_port: int = 8765,
) -> dict:
    core_manifest = core_manager.start(port=core_port)
    core_manager.wait_until_ready(port=core_port)
    manifest = manager.start(
        port=bridge_port,
        purpose="approval-resume-live",
        qq_enabled=False,
        app_server_url=None,
        core_mode="dedicated-ws",
        core_url=core_manifest.url,
    )
    manager.wait_until_healthy(manifest.run_id)
    try:
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-resume",
            user_id="debug-user",
            text=f"/cwd {manifest.cwd}",
        )
        client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-resume",
            user_id="debug-user",
            text="/new",
        )
        prompt = (
            "Run the PowerShell command `Get-Date` in the current workspace and tell me the result. "
            "Use the available tools if needed."
        )
        initial_response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-resume",
            user_id="debug-user",
            text=prompt,
        )
        pending = inspector.wait_for_pending_requests(
            manifest,
            "debug",
            "conv-approval-resume",
            timeout_s=60.0,
            interval_s=0.5,
        )
        launch_snapshot_path = Path(manifest.run_dir) / "current" / "launch.json"
        restart = executor.restart(launch_snapshot_path, timeout_s=30.0)
        approve_response = client.send(
            manifest=manifest,
            channel_id="debug",
            conversation_id="conv-approval-resume",
            user_id="debug-user",
            text="/approve",
        )
        after = inspector.wait_until_no_pending_requests(
            manifest,
            "debug",
            "conv-approval-resume",
            timeout_s=30.0,
            interval_s=0.5,
        )
        return {
            "core": core_manifest.to_dict(),
            "manifest": manifest.to_dict(),
            "initial_response": initial_response,
            "pending": pending,
            "restart": restart,
            "approve_response": approve_response,
            "after": after,
        }
    finally:
        manager.stop(manifest.run_id)
        core_manager.stop()

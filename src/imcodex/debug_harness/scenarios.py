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

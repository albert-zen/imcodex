from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ..sdk_runtime import sdk_health_payload


def install_debug_routes(app: FastAPI, runtime) -> None:
    """Expose bounded SDK diagnostics without duplicating runtime state."""

    @app.get("/api/debug/runtime")
    async def debug_runtime() -> dict[str, Any]:
        gateway = getattr(runtime, "gateway", None)
        snapshot = gateway.diagnostics() if gateway is not None else None
        observability = getattr(runtime, "observability", None)
        return {
            "instance_id": getattr(getattr(observability, "context", None), "instance_id", None),
            "health": sdk_health_payload(snapshot) if snapshot is not None else None,
            "appserver": {
                "connection": dict(
                    getattr(getattr(runtime, "client", None), "connection_facts", lambda: {})()
                ),
                "ownership": "sdk-application",
            },
            "managed_channels": [
                getattr(channel, "channel_instance_id", "unknown")
                for channel in getattr(runtime, "managed_channels", [])
            ],
        }

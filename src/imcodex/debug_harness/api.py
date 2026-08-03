from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..sdk_runtime import sdk_health_payload


def install_debug_routes(app: FastAPI, runtime) -> None:
    """Expose read-only product diagnostics without creating runtime truth."""

    @app.get("/api/debug/runtime")
    async def debug_runtime() -> dict[str, Any]:
        observability = getattr(runtime, "observability", None)
        health = _read_json(
            getattr(getattr(observability, "paths", None), "current_health_path", None)
        )
        diagnostics = None
        composition = getattr(runtime, "composition", None)
        gateway = getattr(composition, "gateway", None)
        provider = getattr(gateway, "diagnostics_snapshot", None)
        if callable(provider):
            diagnostics = sdk_health_payload(provider())
        return {
            "instance_id": getattr(
                getattr(observability, "context", None), "instance_id", None
            ),
            "health": health,
            "sdk": diagnostics,
        }


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .models import DebugRunManifest


class DebugHarnessInspector:
    def __init__(self, *, http_client: Any | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=30.0)

    def inspect_run(self, manifest: DebugRunManifest, *, tail: int = 20) -> dict[str, Any]:
        current = Path(manifest.run_dir) / "current"
        return {
            "manifest": manifest.to_dict(),
            "instance": self._read_json(current / "instance.json"),
            "health": self._read_json(current / "health.json"),
            "events": self.tail_events(manifest, tail=tail),
        }

    def inspect_runtime_state(self, manifest: DebugRunManifest) -> dict[str, Any]:
        response = self.http_client.get(f"http://127.0.0.1:{manifest.port}/api/debug/runtime")
        response.raise_for_status()
        return response.json()

    def inspect_conversation(self, manifest: DebugRunManifest, channel_id: str, conversation_id: str) -> dict[str, Any]:
        response = self.http_client.get(
            f"http://127.0.0.1:{manifest.port}/api/debug/conversation/{channel_id}/{conversation_id}"
        )
        response.raise_for_status()
        return response.json()

    def inspect_thread(self, manifest: DebugRunManifest, thread_id: str) -> dict[str, Any]:
        response = self.http_client.get(f"http://127.0.0.1:{manifest.port}/api/debug/thread/{thread_id}")
        response.raise_for_status()
        return response.json()

    def tail_events(self, manifest: DebugRunManifest, *, tail: int = 20, prefix: str | None = None) -> list[dict[str, Any]]:
        current = Path(manifest.run_dir) / "current" / "events.jsonl"
        if not current.exists():
            return []
        events = [
            json.loads(line)
            for line in current.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if prefix:
            events = [event for event in events if str(event.get("event") or "").startswith(prefix)]
        if tail <= 0:
            return events
        return events[-tail:]

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

from __future__ import annotations

import json
from threading import Lock
from typing import Any

from .context import InstanceContext
from .paths import ObservabilityPaths


class HealthWriter:
    def __init__(self, *, paths: ObservabilityPaths, context: InstanceContext, clock) -> None:
        self.paths = paths
        self.context = context
        self.clock = clock
        self._lock = Lock()
        self._state: dict[str, Any] = {
            "instance_id": context.instance_id,
            "status": "starting",
            "http": {"listening": False, "host": context.http_host, "port": context.http_port},
            "channels": {},
            "appserver": {"connected": False, "mode": None},
            "updated_at": self.clock().astimezone().isoformat(),
        }
        self.write()

    def update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                self._state[key] = value
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._write_locked()

    def merge_channel(self, channel_id: str, **changes: Any) -> None:
        with self._lock:
            channel_state = dict(self._state.get("channels") or {})
            current = dict(channel_state.get(channel_id) or {})
            current.update(changes)
            channel_state[channel_id] = current
            self._state["channels"] = channel_state
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._write_locked()

    def merge_http(self, **changes: Any) -> None:
        with self._lock:
            current = dict(self._state.get("http") or {})
            current.update(changes)
            self._state["http"] = current
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._write_locked()

    def merge_appserver(self, **changes: Any) -> None:
        with self._lock:
            current = dict(self._state.get("appserver") or {})
            current.update(changes)
            self._state["appserver"] = current
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._write_locked()

    def write(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=True, indent=2)
        for path in (self.paths.health_path, self.paths.current_health_path):
            path.write_text(payload + "\n", encoding="utf-8")

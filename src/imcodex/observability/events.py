from __future__ import annotations

import json
from threading import Lock
from typing import Any

from .context import InstanceContext
from .paths import ObservabilityPaths


class EventWriter:
    def __init__(self, *, paths: ObservabilityPaths, context: InstanceContext, clock) -> None:
        self.paths = paths
        self.context = context
        self.clock = clock
        self._lock = Lock()

    def emit(
        self,
        *,
        component: str,
        event: str,
        level: str = "INFO",
        message: str = "",
        data: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        record = {
            "ts": self.clock().astimezone().isoformat(),
            "level": level,
            "component": component,
            "event": event,
            "message": message,
            "instance_id": self.context.instance_id,
            "pid": self.context.pid,
        }
        if fields:
            record.update({key: value for key, value in fields.items() if value is not None})
        if data is not None:
            record["data"] = data
        payload = json.dumps(record, ensure_ascii=True)
        with self._lock:
            for path in (self.paths.events_path, self.paths.current_events_path):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(payload + "\n")

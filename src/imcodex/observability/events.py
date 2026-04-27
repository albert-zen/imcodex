from __future__ import annotations

import json
from queue import Queue
from threading import Lock
from threading import Thread
from typing import Any

from .context import InstanceContext
from .paths import ObservabilityPaths


class EventWriter:
    def __init__(self, *, paths: ObservabilityPaths, context: InstanceContext, clock) -> None:
        self.paths = paths
        self.context = context
        self.clock = clock
        self._lock = Lock()
        self._state_lock = Lock()
        self._queue: Queue[dict[str, Any] | None] = Queue()
        self._closed = False
        for path in (self.paths.events_path, self.paths.current_events_path):
            path.touch(exist_ok=True)
        self._thread = Thread(target=self._write_loop, name="imcodex-event-writer", daemon=True)
        self._thread.start()

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
        with self._state_lock:
            if self._closed:
                return
            self._queue.put(record)

    def flush(self) -> None:
        self._queue.join()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._queue.join()
        self._thread.join(timeout=1)

    def _write_loop(self) -> None:
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    return
                try:
                    self._write_payload(json.dumps(record, ensure_ascii=True))
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def _write_payload(self, payload: str) -> None:
        with self._lock:
            for path in (self.paths.events_path, self.paths.current_events_path):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(payload + "\n")

from __future__ import annotations

import json
from queue import Queue
from threading import Lock
from threading import Thread
from typing import Any

from .context import InstanceContext
from .paths import ObservabilityPaths


class RawProtocolWriter:
    def __init__(self, *, paths: ObservabilityPaths, context: InstanceContext, clock) -> None:
        self.paths = paths
        self.context = context
        self.clock = clock
        self._lock = Lock()
        self._state_lock = Lock()
        self._queue: Queue[str | None] = Queue()
        self._closed = False
        for path in (self.paths.raw_protocol_path, self.paths.current_raw_protocol_path):
            path.touch(exist_ok=True)
        self._thread = Thread(target=self._write_loop, name="imcodex-raw-protocol-writer", daemon=True)
        self._thread.start()

    def emit(
        self,
        *,
        stage: str,
        connection_mode: str,
        connection_epoch: int,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "ts": self.clock().astimezone().isoformat(),
            "stage": stage,
            "instance_id": self.context.instance_id,
            "pid": self.context.pid,
            "connection_mode": connection_mode,
            "connection_epoch": connection_epoch,
            "payload": payload,
        }
        try:
            serialized = json.dumps(record, ensure_ascii=False)
        except Exception:
            return
        with self._state_lock:
            if self._closed:
                return
            self._queue.put(serialized)

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
            serialized = self._queue.get()
            try:
                if serialized is None:
                    return
                try:
                    self._write_payload(serialized)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def _write_payload(self, payload: str) -> None:
        with self._lock:
            for path in (self.paths.raw_protocol_path, self.paths.current_raw_protocol_path):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(payload + "\n")

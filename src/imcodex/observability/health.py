from __future__ import annotations

import copy
import json
from queue import Empty, Full, Queue
from threading import Lock
from threading import Thread
import time
from typing import Any

from .context import InstanceContext
from .paths import ObservabilityPaths


class HealthWriter:
    CLOSE_TIMEOUT_S = 0.25

    def __init__(self, *, paths: ObservabilityPaths, context: InstanceContext, clock) -> None:
        self.paths = paths
        self.context = context
        self.clock = clock
        self._lock = Lock()
        self._queue: Queue[tuple[dict[str, Any], bool]] = Queue(maxsize=1)
        self._closed = False
        self._state: dict[str, Any] = {
            "instance_id": context.instance_id,
            "status": "starting",
            "http": {"listening": False, "host": context.http_host, "port": context.http_port},
            "channels": {},
            "appserver": {"connected": False, "mode": None},
            "updated_at": self.clock().astimezone().isoformat(),
        }
        self._write_payload(copy.deepcopy(self._state))
        self._thread = Thread(target=self._write_loop, name="imcodex-health-writer", daemon=True)
        self._thread.start()

    def update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                self._state[key] = value
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._enqueue_locked()

    def merge_channel(self, channel_id: str, **changes: Any) -> None:
        with self._lock:
            channel_state = dict(self._state.get("channels") or {})
            current = dict(channel_state.get(channel_id) or {})
            current.update(changes)
            channel_state[channel_id] = current
            self._state["channels"] = channel_state
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._enqueue_locked()

    def merge_http(self, **changes: Any) -> None:
        with self._lock:
            current = dict(self._state.get("http") or {})
            current.update(changes)
            self._state["http"] = current
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._enqueue_locked()

    def merge_appserver(self, **changes: Any) -> None:
        with self._lock:
            current = dict(self._state.get("appserver") or {})
            current.update(changes)
            self._state["appserver"] = current
            self._state["updated_at"] = self.clock().astimezone().isoformat()
            self._enqueue_locked()

    def write(self) -> None:
        with self._lock:
            self._enqueue_locked()

    def flush(self, *, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            final_snapshot = copy.deepcopy(self._state)
            try:
                self._queue.put_nowait((final_snapshot, True))
            except Full:
                try:
                    self._queue.get_nowait()
                except Empty:
                    pass
                else:
                    self._queue.task_done()
                self._queue.put_nowait((final_snapshot, True))
        self._thread.join(timeout=self.CLOSE_TIMEOUT_S)

    def _enqueue_locked(self) -> None:
        if self._closed:
            return
        snapshot = copy.deepcopy(self._state)
        try:
            self._queue.put_nowait((snapshot, False))
        except Full:
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            else:
                self._queue.task_done()
            try:
                self._queue.put_nowait((snapshot, False))
            except Full:
                # The writer won the race and a newer state is already queued.
                pass

    def _write_loop(self) -> None:
        while True:
            snapshot, stop_after_write = self._queue.get()
            try:
                try:
                    self._write_payload(snapshot)
                except Exception:
                    pass
                if stop_after_write:
                    return
            finally:
                self._queue.task_done()

    def _write_payload(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=True, indent=2)
        for path in (self.paths.health_path, self.paths.current_health_path):
            path.write_text(payload + "\n", encoding="utf-8")

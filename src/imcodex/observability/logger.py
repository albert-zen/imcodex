from __future__ import annotations

import logging
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
import time

from ..logging_utils import harden_transport_logging


class _InstanceContextFilter(logging.Filter):
    def __init__(self, instance_id: str) -> None:
        super().__init__()
        self.instance_id = instance_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "instance_id"):
            record.instance_id = self.instance_id
        return True


class _AsyncFanoutHandler(logging.Handler):
    """Bounded logging fanout that keeps file and stderr I/O off asyncio loops."""

    QUEUE_LIMIT = 4096
    CLOSE_TIMEOUT_S = 0.25

    def __init__(self, targets: list[logging.Handler], *, level: int) -> None:
        super().__init__(level=level)
        self.targets = targets
        self._queue: Queue[logging.LogRecord | None] = Queue(maxsize=self.QUEUE_LIMIT)
        self._state_lock = Lock()
        self._closed = False
        self.dropped_records = 0
        self._thread = Thread(target=self._write_loop, name="imcodex-log-writer", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        with self._state_lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(record)
            except Full:
                self.dropped_records += 1

    def flush(self) -> None:
        deadline = time.monotonic() + 1.0
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.put_nowait(None)
            except Full:
                try:
                    self._queue.get_nowait()
                except Empty:
                    pass
                else:
                    self._queue.task_done()
                    self.dropped_records += 1
                self._queue.put_nowait(None)
        self._thread.join(timeout=self.CLOSE_TIMEOUT_S)
        if not self._thread.is_alive():
            closer = Thread(target=self._close_targets, name="imcodex-log-closer", daemon=True)
            closer.start()
            closer.join(timeout=self.CLOSE_TIMEOUT_S)
        super().close()

    def _close_targets(self) -> None:
        for target in self.targets:
            try:
                target.close()
            except Exception:
                pass

    def _write_loop(self) -> None:
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    return
                for target in self.targets:
                    try:
                        target.handle(record)
                    except Exception:
                        pass
            finally:
                self._queue.task_done()


def reset_observability_logging() -> None:
    root = logging.getLogger()
    managed_handlers = [handler for handler in root.handlers if getattr(handler, "_imcodex_managed", False)]
    for handler in managed_handlers:
        root.removeHandler(handler)
        handler.close()


def configure_observability_logging(*, level: str, instance_id: str, log_paths: list[Path]) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved_level)
    harden_transport_logging()
    reset_observability_logging()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(instance_id)s] %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = _InstanceContextFilter(instance_id)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(resolved_level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)
    targets: list[logging.Handler] = [stream_handler]

    for index, path in enumerate(log_paths):
        mode = "w" if index == len(log_paths) - 1 else "a"
        file_handler = logging.FileHandler(path, mode=mode, encoding="utf-8", delay=True)
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        targets.append(file_handler)

    queue_handler = _AsyncFanoutHandler(targets, level=resolved_level)
    queue_handler._imcodex_managed = True  # type: ignore[attr-defined]
    root.addHandler(queue_handler)

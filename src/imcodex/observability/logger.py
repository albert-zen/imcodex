from __future__ import annotations

import logging
from pathlib import Path


class _InstanceContextFilter(logging.Filter):
    def __init__(self, instance_id: str) -> None:
        super().__init__()
        self.instance_id = instance_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "instance_id"):
            record.instance_id = self.instance_id
        return True


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
    stream_handler._imcodex_managed = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    for index, path in enumerate(log_paths):
        mode = "w" if index == len(log_paths) - 1 else "a"
        file_handler = logging.FileHandler(path, mode=mode, encoding="utf-8", delay=True)
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        file_handler._imcodex_managed = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

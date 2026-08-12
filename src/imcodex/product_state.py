from __future__ import annotations

"""Small IM-only preference state.

Native application, project, thread, turn, request, and transcript state is
owned by the SDK/Application graph.  This file deliberately stores only
presentation preferences that have no native counterpart.
"""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any


class ProductState:
    """Persist bounded IM presentation preferences without native identity."""

    _MAX_CONVERSATIONS = 16_384

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._values: dict[str, dict[str, Any]] = {}
        self._load()

    def get(self, channel_instance_id: str, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._values.get(self._key(channel_instance_id, conversation_id), {}))

    def update(
        self,
        channel_instance_id: str,
        conversation_id: str,
        **values: Any,
    ) -> dict[str, Any]:
        key = self._key(channel_instance_id, conversation_id)
        with self._lock:
            current = dict(self._values.get(key, {}))
            for name, value in values.items():
                if value is None:
                    current.pop(name, None)
                else:
                    current[name] = value
            self._values[key] = current
            while len(self._values) > self._MAX_CONVERSATIONS:
                del self._values[next(iter(self._values))]
            self._write_locked()
            return dict(current)

    def close(self) -> None:
        """Release the product-only state seam.

        ProductState has no open descriptor or background worker.  The
        explicit no-op keeps lifecycle ownership symmetric with the SDK
        resources without pretending this file is a native state store.
        """

        return None

    @staticmethod
    def _key(channel_instance_id: str, conversation_id: str) -> str:
        return f"{channel_instance_id}\x00{conversation_id}"

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        values = payload.get("conversations") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return
        self._values = {
            str(key): dict(value)
            for key, value in values.items()
            if isinstance(value, dict)
        }

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "conversations": self._values}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

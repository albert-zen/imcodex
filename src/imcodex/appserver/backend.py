from __future__ import annotations

from ..store import ConversationStore
from .backend_errors import CodexBackendErrorMixin
from .backend_types import (
    ACTIVE_THREAD_STATUSES as ACTIVE_THREAD_STATUSES,
)
from .backend_types import (
    StaleThreadBindingError as StaleThreadBindingError,
)
from .backend_types import (
    ThreadListResult as ThreadListResult,
)
from .backend_types import (
    ThreadSelectionError as ThreadSelectionError,
)
from .settings_backend import (
    PERMISSION_MODE_PROFILE_IDS as PERMISSION_MODE_PROFILE_IDS,
)
from .settings_backend import (
    CodexSettingsBackendMixin,
)
from .thread_backend import CodexThreadBackendMixin


class CodexBackend(
    CodexThreadBackendMixin, CodexSettingsBackendMixin, CodexBackendErrorMixin
):
    def __init__(
        self,
        *,
        client,
        store: ConversationStore,
        service_name: str,
    ) -> None:
        self.client = client
        self.store = store
        self.service_name = service_name
        # A native thread has no resumable rollout until its first turn starts.
        # Keep that transient fact process-local so the first input can use the
        # live connection instead of asking Codex to resume nonexistent history.
        self._unpersisted_thread_ids: set[str] = set()

    def prefers_native_recovery(self) -> bool:
        preserves_server_state = getattr(self.client, "preserves_server_state", None)
        if preserves_server_state is not None:
            return bool(preserves_server_state)
        mode = getattr(self.client, "connection_mode", "") or getattr(
            self.client, "last_connection_mode", ""
        )
        if mode == "disconnected":
            mode = getattr(self.client, "last_connection_mode", "")
        return mode in {"external", "dedicated-ws", "shared-ws"}

    def app_server_connection_facts(self) -> dict:
        provider = getattr(self.client, "connection_facts", None)
        if callable(provider):
            return dict(provider())
        mode = str(getattr(self.client, "connection_mode", "") or "disconnected")
        connected = mode != "disconnected"
        if mode in {"external", "dedicated-ws", "shared-ws"}:
            ownership = "external"
        elif mode == "spawned-stdio":
            ownership = "bridge-child"
        else:
            ownership = "unknown"
        return {
            "connected": connected,
            "ready": connected and bool(getattr(self.client, "initialized", False)),
            "status": "connected" if connected else "disconnected",
            "mode": mode,
            "ownership": ownership,
            "transport": "unknown",
            "endpoint": "(unknown)",
            "connection_epoch": int(getattr(self.client, "connection_epoch", 0) or 0),
            "reconnect_enabled": self.prefers_native_recovery(),
        }

from __future__ import annotations

from .session_registry import SessionRegistry


class VisibilityClassifier:
    def __init__(self, session_registry=None) -> None:
        self.session_registry = session_registry

    def should_emit(self, category: str, *, thread_id: str, store) -> bool:
        if category == "final":
            return True
        binding = None
        if self.session_registry is None and store is not None:
            self.session_registry = SessionRegistry(store)
        if thread_id and self.session_registry is not None:
            binding = self.session_registry.find_routing_binding(thread_id)
        if binding is None:
            return category != "toolcall"
        if category == "commentary":
            return binding.show_commentary
        if category == "toolcall":
            return binding.show_toolcalls
        return True

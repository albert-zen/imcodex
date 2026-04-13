from __future__ import annotations


class VisibilityClassifier:
    def __init__(self, session_registry=None) -> None:
        self.session_registry = session_registry

    def should_emit(self, category: str, *, thread_id: str, store) -> bool:
        if category == "final":
            return True
        binding = None
        if thread_id:
            if self.session_registry is not None:
                binding = self.session_registry.find_routing_binding(thread_id)
            elif store is not None:
                binding = store.find_binding_for_thread(thread_id)
        if binding is None:
            return category != "toolcall"
        if category == "commentary":
            return binding.show_commentary
        if category == "toolcall":
            return binding.show_toolcalls
        return True

from __future__ import annotations


class VisibilityClassifier:
    def should_emit(self, category: str, *, thread_id: str, store) -> bool:
        if category == "final":
            return True
        binding = store.find_binding_for_thread(thread_id) if store is not None and thread_id else None
        if binding is None:
            return category != "toolcall"
        if category == "commentary":
            return binding.show_commentary
        if category == "toolcall":
            return binding.show_toolcalls
        return True

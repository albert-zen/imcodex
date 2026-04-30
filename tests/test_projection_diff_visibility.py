from __future__ import annotations

from imcodex.bridge import MessageProjector
from imcodex.store import ConversationStore


def test_diff_update_hidden_by_standard_visibility() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    projector = MessageProjector()

    message = projector.project_notification(_diff_update(), store)

    assert message is None


def test_diff_update_visible_when_toolcalls_are_shown() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    store.set_bootstrap_cwd("qq", "conv-1", r"D:\work\alpha")
    store.bind_thread("qq", "conv-1", "thr_1")
    store.set_toolcall_visibility("qq", "conv-1", enabled=True)
    projector = MessageProjector()

    message = projector.project_notification(_diff_update(), store)

    assert message is not None
    assert message.message_type == "turn_progress"
    assert "Diff updated." in message.text
    assert "src/imcodex/bridge/core.py" in message.text


def _diff_update() -> dict:
    return {
        "method": "turn/diff/updated",
        "params": {
            "threadId": "thr_1",
            "turnId": "turn_1",
            "files": ["src/imcodex/bridge/core.py"],
        },
    }

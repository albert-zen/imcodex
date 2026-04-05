from __future__ import annotations

from imcodex.bridge import BridgeService, CommandRouter, MessageProjector


def test_bridge_layer_exports_core_types() -> None:
    assert BridgeService.__module__ == "imcodex.bridge.core"
    assert CommandRouter.__module__ == "imcodex.bridge.commands"
    assert MessageProjector.__module__ == "imcodex.bridge.projection"

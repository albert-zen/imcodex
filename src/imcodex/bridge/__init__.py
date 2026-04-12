from __future__ import annotations

from importlib import import_module

__all__ = [
    "BridgeService",
    "CommandResponse",
    "CommandRouter",
    "MessageProjector",
    "NativeThreadSnapshot",
    "ParsedCommand",
    "RequestRecord",
    "RequestRegistry",
    "SessionRecord",
    "SessionRegistry",
    "ThreadDirectory",
    "TurnStateMachine",
    "TurnStateRecord",
    "parse_command",
]


_EXPORTS = {
    "BridgeService": (".core", "BridgeService"),
    "CommandResponse": (".commands", "CommandResponse"),
    "CommandRouter": (".commands", "CommandRouter"),
    "ParsedCommand": (".commands", "ParsedCommand"),
    "parse_command": (".commands", "parse_command"),
    "MessageProjector": (".projection", "MessageProjector"),
    "RequestRecord": (".request_registry", "RequestRecord"),
    "RequestRegistry": (".request_registry", "RequestRegistry"),
    "SessionRecord": (".session_registry", "SessionRecord"),
    "SessionRegistry": (".session_registry", "SessionRegistry"),
    "NativeThreadSnapshot": (".thread_directory", "NativeThreadSnapshot"),
    "ThreadDirectory": (".thread_directory", "ThreadDirectory"),
    "TurnStateMachine": (".turn_state", "TurnStateMachine"),
    "TurnStateRecord": (".turn_state", "TurnStateRecord"),
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attr_name = _EXPORTS[name]
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

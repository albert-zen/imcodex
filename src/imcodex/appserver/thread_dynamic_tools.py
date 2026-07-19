from __future__ import annotations

import copy
from typing import Any


NATIVE_THREAD_DYNAMIC_TOOL_NAMES = frozenset(
    {
        "create_thread",
        "list_threads",
        "read_thread",
        "send_message_to_thread",
    }
)

_THINKING_EFFORTS = [
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]

_NATIVE_THREAD_DYNAMIC_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": "list_threads",
        "description": (
            "List recent Codex threads on the current native App Server. "
            "Use an optional query to find a thread before reading or messaging it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional thread search query."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum number of thread summaries to return.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_thread",
        "description": (
            "Read recent status and turn summaries for one Codex thread without switching to it. "
            "Use page cursors from earlier responses to read older turns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Thread id to inspect."},
                "hostId": {
                    "type": "string",
                    "description": "Optional host id returned by list_threads; only local is supported.",
                },
                "cursor": {"type": "string", "description": "Optional cursor for older turns."},
                "turnLimit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum number of turns to return.",
                },
                "includeOutputs": {
                    "type": "boolean",
                    "description": "Whether to include truncated tool or command outputs.",
                },
                "maxOutputCharsPerItem": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20000,
                    "description": "Maximum output characters to keep per included output item.",
                },
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "send_message_to_thread",
        "description": (
            "Send a follow-up prompt to an existing Codex thread. "
            "Omit model and thinking to keep that thread's current settings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Thread id to continue."},
                "hostId": {
                    "type": "string",
                    "description": "Optional host id returned by list_threads; only local is supported.",
                },
                "prompt": {"type": "string", "description": "Follow-up prompt to send."},
                "model": {"type": "string", "description": "Optional model override."},
                "thinking": {
                    "type": "string",
                    "enum": _THINKING_EFFORTS,
                    "description": "Optional reasoning effort override.",
                },
            },
            "required": ["threadId", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "create_thread",
        "description": (
            "Create a separate Codex thread in the calling thread's working directory and start it "
            "with an initial prompt. Use only when a separate or background thread is requested."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Initial prompt for the new thread."},
                "model": {"type": "string", "description": "Optional model override."},
                "thinking": {
                    "type": "string",
                    "enum": _THINKING_EFFORTS,
                    "description": "Optional reasoning effort override.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
)


def native_thread_dynamic_tool_specs() -> list[dict[str, Any]]:
    """Return a fresh protocol payload for native thread-management tools."""

    return copy.deepcopy(list(_NATIVE_THREAD_DYNAMIC_TOOLS))

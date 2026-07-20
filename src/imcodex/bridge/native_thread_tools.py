from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ..appserver.client import AppServerError
from ..appserver.thread_dynamic_tools import NATIVE_THREAD_DYNAMIC_TOOL_NAMES


LOCAL_HOST_ID = "local"
DEFAULT_THREAD_LIMIT = 10
DEFAULT_TURN_LIMIT = 1
DEFAULT_MAX_OUTPUT_CHARS = 2_000
MAX_MODEL_TEXT_CHARS = 8_000
MAX_COLLECTION_ITEMS = 100
MAX_DYNAMIC_TOOL_RESPONSE_CHARS = 128_000
MAX_FAILURE_MESSAGE_CHARS = 2_000

NATIVE_THREAD_DYNAMIC_TOOLS = NATIVE_THREAD_DYNAMIC_TOOL_NAMES | frozenset(
    {
        "fork_thread",
        "set_thread_archived",
        "set_thread_title",
    }
)

# These tools are registered by Codex Desktop alongside the native-mappable
# tools above. They require Desktop-owned project, pin, or handoff state and
# therefore must not be emulated by the bridge.
DESKTOP_ONLY_THREAD_DYNAMIC_TOOLS = frozenset(
    {
        "get_handoff_status",
        "handoff_thread",
        "list_projects",
        "set_thread_pinned",
    }
)

KNOWN_THREAD_DYNAMIC_TOOLS = NATIVE_THREAD_DYNAMIC_TOOLS | DESKTOP_ONLY_THREAD_DYNAMIC_TOOLS


class _CreatedThreadPostStartError(RuntimeError):
    def __init__(
        self,
        *,
        thread_id: str,
        phase: str,
        initial_turn_status: str,
        cause: Exception,
    ) -> None:
        super().__init__(str(cause))
        self.thread_id = thread_id
        self.phase = phase
        self.initial_turn_status = initial_turn_status


class NativeThreadToolAdapter:
    """Translate Desktop thread tools to native App Server requests.

    The adapter intentionally owns no thread state. Native Codex remains the
    source of truth; this class only reshapes request and response payloads.
    """

    def __init__(self, *, backend) -> None:
        self.backend = backend

    async def call(self, tool: str, arguments: object, *, source_thread_id: str) -> dict[str, Any]:
        if tool in DESKTOP_ONLY_THREAD_DYNAMIC_TOOLS:
            return self.failure(
                f"{tool} requires Codex Desktop-owned state and is unavailable on the independent App Server."
            )
        if tool not in NATIVE_THREAD_DYNAMIC_TOOLS:
            raise KeyError(tool)
        if not isinstance(arguments, dict):
            return self.failure(f"{tool} received invalid arguments.")
        try:
            result = await getattr(self, f"_{tool}")(arguments, source_thread_id=source_thread_id)
        except _CreatedThreadPostStartError as exc:
            return self.failure(
                json.dumps(
                    {
                        "threadId": exc.thread_id,
                        "phase": exc.phase,
                        "initialTurnStatus": exc.initial_turn_status,
                        "error": (str(exc) or "The initial turn could not be confirmed.")[:1000],
                        "recovery": (
                            "The thread was created. Inspect or message this thread instead of "
                            "retrying create_thread blindly."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        except (AppServerError, ValueError) as exc:
            return self.failure(str(exc) or f"{tool} failed.")
        return self.success(result)

    async def _list_threads(self, arguments: dict, *, source_thread_id: str) -> dict:
        del source_thread_id
        self._reject_unknown(arguments, {"query", "limit"})
        query = self._optional_string(arguments, "query")
        limit = self._bounded_int(arguments, "limit", default=DEFAULT_THREAD_LIMIT, minimum=1, maximum=50)
        params: dict[str, Any] = {
            "limit": limit,
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": ["cli", "vscode", "appServer"],
        }
        if query:
            params["searchTerm"] = query
        result = await self.backend.call_native("thread/list", params)
        threads = self._list_data(result)
        return {
            "schemaVersion": 2,
            "query": query or None,
            "threads": [self._thread_summary(thread) for thread in threads[:limit]],
            "unavailableHosts": [],
        }

    async def _read_thread(self, arguments: dict, *, source_thread_id: str) -> dict:
        del source_thread_id
        self._reject_unknown(
            arguments,
            {"threadId", "hostId", "cursor", "turnLimit", "includeOutputs", "maxOutputCharsPerItem"},
        )
        thread_id = self._required_string(arguments, "threadId")
        self._require_local_host(arguments)
        cursor = self._optional_string(arguments, "cursor")
        turn_limit = self._bounded_int(
            arguments,
            "turnLimit",
            default=DEFAULT_TURN_LIMIT,
            minimum=1,
            maximum=10,
        )
        include_outputs = self._optional_bool(arguments, "includeOutputs", default=False)
        max_output_chars = self._bounded_int(
            arguments,
            "maxOutputCharsPerItem",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=0,
            maximum=20_000,
        )
        metadata_result = await self.backend.call_native(
            "thread/read",
            {"threadId": thread_id},
        )
        thread = metadata_result.get("thread")
        if not isinstance(thread, dict):
            raise ValueError(f"Thread {thread_id} is not available in Codex.")
        turns_params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": turn_limit,
            "sortDirection": "desc",
            "itemsView": "full" if include_outputs else "summary",
        }
        if cursor is not None:
            turns_params["cursor"] = cursor
        turns_result = await self.backend.call_native("thread/turns/list", turns_params)
        selected = self._list_data(turns_result)
        next_cursor = turns_result.get("nextCursor")
        return {
            "schemaVersion": 1,
            "thread": self._read_thread_summary(thread),
            "page": {
                "order": "newest_first",
                "limit": turn_limit,
                "nextCursor": str(next_cursor) if next_cursor is not None else None,
                "hasMore": next_cursor is not None,
            },
            "turns": [
                self._trim_turn(turn, include_outputs=include_outputs, max_output_chars=max_output_chars)
                for turn in selected
            ],
        }

    async def _send_message_to_thread(self, arguments: dict, *, source_thread_id: str) -> dict:
        del source_thread_id
        self._reject_unknown(arguments, {"threadId", "hostId", "prompt", "model", "thinking"})
        thread_id = self._required_string(arguments, "threadId")
        prompt = self._required_string(arguments, "prompt")
        self._require_local_host(arguments)
        model = self._optional_string(arguments, "model")
        effort = self._optional_string(arguments, "thinking")
        resume = await self.backend.call_native("thread/resume", {"threadId": thread_id})
        thread = resume.get("thread")
        if not isinstance(thread, dict):
            raise ValueError(f"Thread {thread_id} is not available in Codex.")
        active_turn_id = self._active_turn_id(thread)
        if active_turn_id:
            if model is not None or effort is not None:
                raise ValueError(
                    "send_message_to_thread cannot apply model or thinking overrides while the target turn is active."
                )
            await self.backend.call_native(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": active_turn_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
        else:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "summary": "concise",
            }
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            await self.backend.call_native("turn/start", params)
        return {"threadId": thread_id}

    async def _create_thread(self, arguments: dict, *, source_thread_id: str) -> dict:
        self._reject_unknown(arguments, {"prompt", "model", "thinking"})
        if not source_thread_id:
            raise ValueError("create_thread missing calling thread id.")
        prompt = self._required_string(arguments, "prompt")
        model = self._optional_string(arguments, "model")
        effort = self._optional_string(arguments, "thinking")
        source_result = await self.backend.call_native(
            "thread/read",
            {"threadId": source_thread_id},
        )
        source = source_result.get("thread")
        if not isinstance(source, dict):
            raise ValueError(f"Thread {source_thread_id} is not available in Codex.")
        cwd = str(source.get("cwd") or "").strip()
        if not cwd:
            raise ValueError("The calling thread has no working directory for a child thread.")
        start_result = await self.backend.call_native(
            "thread/start",
            {
                "cwd": cwd,
                "serviceName": str(getattr(self.backend, "service_name", "imcodex")),
                "dynamicTools": self._configured_dynamic_tools(),
            },
        )
        created = start_result.get("thread")
        if not isinstance(created, dict) or not str(created.get("id") or ""):
            raise ValueError("Codex did not return a newly created thread.")
        thread_id = str(created["id"])
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "summary": "concise",
        }
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort
        try:
            await self.backend.call_native("turn/start", turn_params)
        except Exception as exc:
            raise _CreatedThreadPostStartError(
                thread_id=thread_id,
                phase="initialTurnStart",
                initial_turn_status="unknown",
                cause=exc,
            ) from exc
        return {"threadId": thread_id}

    def _configured_dynamic_tools(self) -> list[dict]:
        dynamic_tools = getattr(self.backend, "thread_dynamic_tools", None)
        if not isinstance(dynamic_tools, list) or not dynamic_tools:
            raise ValueError("The native thread-tool host has no configured child tool set.")
        return copy.deepcopy(dynamic_tools)

    async def _fork_thread(self, arguments: dict, *, source_thread_id: str) -> dict:
        self._reject_unknown(arguments, {"threadId", "environment"})
        thread_id = self._optional_string(arguments, "threadId") or source_thread_id
        if not thread_id:
            raise ValueError("fork_thread missing calling thread id.")
        environment = arguments.get("environment")
        if environment is not None:
            if not isinstance(environment, dict) or set(environment) != {"type"}:
                raise ValueError("fork_thread received invalid arguments.")
            if environment.get("type") != "same-directory":
                raise ValueError("fork_thread worktree mode requires Codex Desktop and is unavailable here.")
        result = await self.backend.call_native("thread/fork", {"threadId": thread_id})
        forked = result.get("thread")
        if not isinstance(forked, dict) or not str(forked.get("id") or ""):
            raise ValueError("Codex did not return a forked thread.")
        return {
            "environment": {"type": "same-directory"},
            "sourceThreadId": thread_id,
            "threadId": str(forked["id"]),
            "continuation": (
                "The fork contains completed history only. Send a follow-up message to threadId "
                "only if the task requires work to continue there."
            ),
        }

    async def _set_thread_archived(self, arguments: dict, *, source_thread_id: str) -> dict:
        self._reject_unknown(arguments, {"threadId", "archived"})
        thread_id = self._optional_string(arguments, "threadId") or source_thread_id
        if not thread_id:
            raise ValueError("set_thread_archived missing calling thread id.")
        archived = self._required_bool(arguments, "archived")
        method = "thread/archive" if archived else "thread/unarchive"
        await self.backend.call_native(method, {"threadId": thread_id})
        return {"threadId": thread_id, "archived": archived}

    async def _set_thread_title(self, arguments: dict, *, source_thread_id: str) -> dict:
        self._reject_unknown(arguments, {"threadId", "title"})
        thread_id = self._optional_string(arguments, "threadId") or source_thread_id
        if not thread_id:
            raise ValueError("set_thread_title missing calling thread id.")
        title = self._required_string(arguments, "title")
        await self.backend.call_native("thread/name/set", {"threadId": thread_id, "name": title})
        return {"threadId": thread_id, "title": title}

    @staticmethod
    def success(value: object) -> dict[str, Any]:
        text = NativeThreadToolAdapter._bounded_json(value, MAX_DYNAMIC_TOOL_RESPONSE_CHARS)
        if text is None:
            return NativeThreadToolAdapter.failure(
                "Native thread tool result exceeded the safe response budget. Narrow the query or turn limit."
            )
        return {
            "contentItems": [{"type": "inputText", "text": text}],
            "success": True,
        }

    @staticmethod
    def failure(message: str) -> dict[str, Any]:
        bounded = str(message)
        if len(bounded) > MAX_FAILURE_MESSAGE_CHARS:
            bounded = bounded[:MAX_FAILURE_MESSAGE_CHARS] + "… [truncated]"
        return {
            "contentItems": [{"type": "inputText", "text": bounded}],
            "success": False,
        }

    @staticmethod
    def _list_data(result: dict) -> list[dict]:
        values = result.get("data")
        if not isinstance(values, list):
            values = result.get("threads")
        return [value for value in values or [] if isinstance(value, dict)]

    @staticmethod
    def _status_type(thread: dict) -> str:
        status = thread.get("status")
        if isinstance(status, dict):
            return str(status.get("type") or "notLoaded")
        return str(status or "notLoaded")

    @classmethod
    def _thread_summary(cls, thread: dict) -> dict:
        thread_id = str(thread.get("id") or "")
        preview = str(thread.get("preview") or "")
        title = str(thread.get("name") or "").strip() or preview.strip() or thread_id
        cwd = str(thread.get("cwd") or "")
        return {
            "id": thread_id,
            "hostId": LOCAL_HOST_ID,
            "title": title,
            "description": Path(cwd).name if cwd else None,
            "preview": preview,
            "status": cls._status_type(thread),
            "hasUnreadTurn": False,
            "cwd": cwd,
            "createdAt": thread.get("createdAt"),
            "updatedAt": thread.get("updatedAt"),
        }

    @classmethod
    def _read_thread_summary(cls, thread: dict) -> dict:
        summary = cls._thread_summary(thread)
        return {
            "id": summary["id"],
            "hostId": summary["hostId"],
            "title": str(thread.get("name") or "").strip() or None,
            "preview": summary["preview"],
            "status": {"type": summary["status"]},
            "cwd": summary["cwd"],
            "createdAt": summary["createdAt"],
            "updatedAt": summary["updatedAt"],
        }

    @classmethod
    def _trim_turn(cls, turn: dict, *, include_outputs: bool, max_output_chars: int) -> dict:
        trimmed = {
            key: cls._bounded_copy(value)
            for key, value in turn.items()
            if key != "items"
        }
        source_items = turn.get("items")
        if not isinstance(source_items, list):
            return trimmed
        if len(source_items) > MAX_COLLECTION_ITEMS:
            head_count = min(10, MAX_COLLECTION_ITEMS)
            source_items = source_items[:head_count] + source_items[-(MAX_COLLECTION_ITEMS - head_count) :]
            trimmed["itemsTruncated"] = True
            trimmed["originalItemCount"] = len(turn.get("items") or [])
        items = [cls._bounded_copy(item) for item in source_items]
        trimmed["items"] = items
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning" and not include_outputs:
                item.pop("content", None)
            output_fields = {
                "commandExecution": ("aggregatedOutput", "output"),
                "mcpToolCall": ("result",),
                "dynamicToolCall": ("contentItems",),
                "imageGeneration": ("result",),
            }.get(str(item_type), ())
            for field in output_fields:
                if include_outputs:
                    cls._truncate_field(item, field, max_output_chars)
                else:
                    item.pop(field, None)
            if item_type == "fileChange":
                changes = item.get("changes")
                if isinstance(changes, list):
                    for change in changes:
                        if not isinstance(change, dict):
                            continue
                        if include_outputs:
                            cls._truncate_field(change, "diff", max_output_chars)
                        else:
                            change.pop("diff", None)
        return trimmed

    @classmethod
    def _bounded_copy(cls, value: Any) -> Any:
        if isinstance(value, str):
            if len(value) <= MAX_MODEL_TEXT_CHARS:
                return value
            return value[:MAX_MODEL_TEXT_CHARS] + "… [truncated]"
        if isinstance(value, dict):
            pairs = list(value.items())[:MAX_COLLECTION_ITEMS]
            return {str(key): cls._bounded_copy(item) for key, item in pairs}
        if isinstance(value, list):
            return [cls._bounded_copy(item) for item in value[:MAX_COLLECTION_ITEMS]]
        return copy.deepcopy(value)

    @staticmethod
    def _bounded_json(value: object, limit: int) -> str | None:
        chunks: list[str] = []
        length = 0
        for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(value):
            length += len(chunk)
            if length > limit:
                return None
            chunks.append(chunk)
        return "".join(chunks)

    @staticmethod
    def _truncate_field(payload: dict, key: str, limit: int) -> None:
        value = payload.get(key)
        if value is None:
            return
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            payload[key] = {
                "text": text[:limit],
                "truncated": True,
                "originalChars": len(text),
            }

    @staticmethod
    def _active_turn_id(thread: dict) -> str | None:
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        for turn in reversed(turns):
            if isinstance(turn, dict) and turn.get("status") == "inProgress":
                turn_id = str(turn.get("id") or "")
                return turn_id or None
        return None

    @staticmethod
    def _reject_unknown(arguments: dict, allowed: set[str]) -> None:
        if set(arguments) - allowed:
            raise ValueError("received invalid arguments.")

    @staticmethod
    def _required_string(arguments: dict, key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string.")
        return value.strip()

    @staticmethod
    def _optional_string(arguments: dict, key: str) -> str | None:
        value = arguments.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
        return value.strip() or None

    @staticmethod
    def _required_bool(arguments: dict, key: str) -> bool:
        value = arguments.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean.")
        return value

    @staticmethod
    def _optional_bool(arguments: dict, key: str, *, default: bool) -> bool:
        value = arguments.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean.")
        return value

    @staticmethod
    def _bounded_int(
        arguments: dict,
        key: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
        return value

    @classmethod
    def _require_local_host(cls, arguments: dict) -> None:
        host_id = cls._optional_string(arguments, "hostId")
        if host_id not in {None, LOCAL_HOST_ID}:
            raise ValueError(f"Remote host {host_id} is unavailable on the independent App Server.")

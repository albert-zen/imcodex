from __future__ import annotations


_HISTORY_TEXT_LIMIT = 1200


def render_thread_history(payload: dict, *, limit: int = 1) -> str:
    turns = _thread_history_items(payload)
    page = max(1, int(payload.get("page") or 1))
    has_older = bool(payload.get("hasOlder") or payload.get("has_older"))
    if not turns:
        return f"## Thread History · Page {page}\n\n_No turns on this page._"
    selected = turns[-limit:]
    count_label = f"{len(selected)} native turn{'s' if len(selected) != 1 else ''}"
    lines = [f"## Thread History · Page {page}", "", f"_{count_label}_"]
    for index, turn in enumerate(selected, start=1):
        turn_id = str(turn.get("id") or turn.get("turnId") or "").strip()
        status = str(turn.get("status") or "").strip()
        details = []
        if status:
            details.append(_human_state(status))
        details.append(f"`{_compact_turn_id(turn_id).replace('`', '')}`")
        user_text = _turn_user_text(turn)
        agent_text = _turn_agent_text(turn)
        if index > 1:
            lines.extend(["", "---"])
        lines.extend(["", f"### {index}. {' · '.join(details)}"])
        if user_text:
            lines.extend(
                [
                    "",
                    "**You**",
                    _blockquote(_compact_history_text(user_text, _HISTORY_TEXT_LIMIT)),
                ]
            )
        if agent_text:
            lines.extend(
                [
                    "",
                    "**Codex**",
                    "",
                    _compact_history_text(agent_text, _HISTORY_TEXT_LIMIT),
                ]
            )
        if not user_text and not agent_text:
            lines.extend(["", "_No user or Codex message._"])
        if _turn_has_compaction(turn):
            lines.extend(["", "_Native context compaction occurred in this turn._"])
        error_text = _turn_error_text(turn)
        if error_text:
            lines.extend(["", "**Error**", "", _compact_history_text(error_text, _HISTORY_TEXT_LIMIT)])
    if has_older:
        lines.extend(["", "---", "", f"Older turns: `/history {limit} --page {page + 1}`"])
    return "\n".join(lines)


def _turn_has_compaction(turn: dict) -> bool:
    return any(str(item.get("type") or "") == "contextCompaction" for item in _turn_items(turn))


def _turn_error_text(turn: dict) -> str:
    error = turn.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("error") or "").strip()
    return str(error or "").strip()


def _thread_history_items(payload: dict) -> list[dict]:
    for key in ("turns", "data"):
        items = payload.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    thread = payload.get("thread")
    if isinstance(thread, dict):
        turns = thread.get("turns")
        if isinstance(turns, list):
            return [item for item in turns if isinstance(item, dict)]
    return []


def _compact_turn_id(turn_id: str) -> str:
    return turn_id[:10] if turn_id else "turn"


def _turn_user_text(turn: dict) -> str:
    for item in _turn_items(turn):
        item_type = str(item.get("type") or item.get("kind") or "").lower()
        if "user" not in item_type:
            continue
        text = _item_text(item)
        if text:
            return text
    return ""


def _turn_agent_text(turn: dict) -> str:
    final_text = ""
    latest_text = ""
    for item in _turn_items(turn):
        item_type = str(item.get("type") or item.get("kind") or "").lower()
        if "agent" not in item_type and "assistant" not in item_type:
            continue
        text = _item_text(item)
        if not text:
            continue
        latest_text = text
        phase = str(item.get("phase") or "").strip().lower()
        if phase == "final_answer" or ("assistant" in item_type and not phase):
            final_text = text
    return final_text or latest_text


def _turn_items(turn: dict) -> list[dict]:
    items = turn.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _item_text(item: dict) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return " ".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _compact_history_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    truncated = text[: max(0, limit - 2)].rstrip()
    open_fence = _open_markdown_fence(truncated)
    if open_fence:
        truncated = f"{truncated}\n{open_fence}"
    return f"{truncated}\n…"


def _open_markdown_fence(value: str) -> str | None:
    open_fence: str | None = None
    for line in value.splitlines():
        candidate = line.lstrip()
        if len(line) - len(candidate) > 3 or not candidate:
            continue
        fence_character = candidate[0]
        if fence_character not in {"`", "~"}:
            continue
        fence_length = len(candidate) - len(candidate.lstrip(fence_character))
        if fence_length < 3:
            continue
        remainder = candidate[fence_length:]
        if open_fence is None:
            open_fence = fence_character * fence_length
        elif (
            fence_character == open_fence[0]
            and fence_length >= len(open_fence)
            and not remainder.strip()
        ):
            open_fence = None
    return open_fence


def _blockquote(value: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def _human_state(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"inprogress", "in_progress", "working", "running"}:
        return "Working"
    if normalized == "completed":
        return "Completed"
    if normalized == "failed":
        return "Failed"
    if normalized in {"interrupted", "cancelled", "canceled"}:
        return "Interrupted" if normalized == "interrupted" else "Cancelled"
    return "Idle" if normalized == "idle" else str(status or "Idle").title()

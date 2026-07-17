from __future__ import annotations


_HISTORY_TEXT_LIMIT = 1200


def render_thread_history(payload: dict, *, limit: int = 1) -> str:
    turns = _thread_history_items(payload)
    if not turns:
        return "\n".join(["Thread History", "(none)"])
    selected = turns[-limit:]
    lines = [f"Thread History ({len(selected)} turn{'s' if len(selected) != 1 else ''})"]
    for index, turn in enumerate(selected, start=1):
        turn_id = str(turn.get("id") or turn.get("turnId") or "").strip()
        status = str(turn.get("status") or "").strip()
        prefix = _compact_turn_id(turn_id)
        if status:
            prefix = f"{prefix} {_human_state(status)}".strip()
        user_text = _turn_user_text(turn)
        agent_text = _turn_agent_text(turn)
        lines.append(f"\n{index}. {prefix}")
        if user_text:
            lines.append(f"User: {_compact_history_text(user_text, _HISTORY_TEXT_LIMIT)}")
        if agent_text:
            lines.append(f"Codex: {_compact_history_text(agent_text, _HISTORY_TEXT_LIMIT)}")
        if not user_text and not agent_text:
            lines.append("(no user or final agent text)")
    return "\n".join(lines)


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
    for item in _turn_items(turn):
        item_type = str(item.get("type") or item.get("kind") or "").lower()
        if "agent" not in item_type and "assistant" not in item_type:
            continue
        text = _item_text(item)
        if not text:
            continue
        phase = str(item.get("phase") or "").strip().lower()
        if phase == "final_answer" or ("assistant" in item_type and not phase):
            final_text = text
    return final_text


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
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _human_state(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"inprogress", "in_progress", "working", "running"}:
        return "Working"
    if normalized == "completed":
        return "Completed"
    if normalized == "failed":
        return "Failed"
    return "Idle" if normalized == "idle" else str(status or "Idle").title()

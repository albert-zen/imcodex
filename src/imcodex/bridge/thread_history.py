from __future__ import annotations


def render_thread_history(payload: dict, *, limit: int = 6) -> str:
    turns = _thread_history_items(payload)
    if not turns:
        return "\n".join(["Thread History", "(none)"])
    lines = ["Thread History"]
    for turn in turns[-limit:]:
        turn_id = str(turn.get("id") or turn.get("turnId") or "").strip()
        status = str(turn.get("status") or "").strip()
        prefix = _compact_turn_id(turn_id)
        if status:
            prefix = f"{prefix} {_human_state(status)}".strip()
        user_text = _first_turn_item_text(turn, role="user")
        agent_text = _first_turn_item_text(turn, role="agent")
        parts = []
        if user_text:
            parts.append(f"User: {_compact_history_text(user_text, 80)}")
        if agent_text:
            parts.append(f"Codex: {_compact_history_text(agent_text, 80)}")
        summary = " | ".join(parts) if parts else "(no text items)"
        lines.append(f"- {prefix}: {summary}")
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


def _first_turn_item_text(turn: dict, *, role: str) -> str:
    for item in _turn_items(turn):
        item_type = str(item.get("type") or item.get("kind") or "").lower()
        if role == "user" and "user" not in item_type:
            continue
        if role == "agent" and "agent" not in item_type and "assistant" not in item_type:
            continue
        text = _item_text(item)
        if text:
            return text
    return ""


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

from __future__ import annotations


def split_text(text: str, *, limit: int) -> list[str]:
    """Split text for an IM platform without splitting Unicode code points."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    remaining = text.strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        minimum_soft_break = max(1, limit // 2)
        break_at = max(
            window.rfind("\n\n", minimum_soft_break, limit + 1),
            window.rfind("\n", minimum_soft_break, limit + 1),
            window.rfind(" ", minimum_soft_break, limit + 1),
        )
        if break_at < minimum_soft_break:
            break_at = limit
        chunk = remaining[:break_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            break_at = limit
        chunks.append(chunk)
        remaining = remaining[break_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

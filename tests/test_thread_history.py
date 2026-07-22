from imcodex.bridge.thread_history import render_thread_history


def test_history_uses_readable_markdown_and_preserves_codex_structure() -> None:
    text = render_thread_history(
        {
            "turns": [
                {
                    "id": "turn_1234567890abcdef",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "text": "Inspect the parser\nand keep the list.",
                        },
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Done:\n\n- Parser checked\n- Tests passed",
                        },
                    ],
                }
            ]
        }
    )

    assert text.startswith("## Thread History · Page 1\n\n_1 native turn_")
    assert "### 1. Completed · `turn_12345`" in text
    assert "**You**\n> Inspect the parser\n> and keep the list." in text
    assert "**Codex**\n\nDone:\n\n- Parser checked\n- Tests passed" in text


def test_history_separates_turns_and_has_a_clear_empty_state() -> None:
    text = render_thread_history(
        {
            "turns": [
                {"id": "turn_1", "status": "completed", "items": []},
                {"id": "turn_2", "status": "completed", "items": []},
            ]
        },
        limit=2,
    )

    assert text.count("\n---\n") == 1
    assert text.count("_No user or Codex message._") == 2
    assert render_thread_history({"turns": []}) == "## Thread History · Page 1\n\n_No turns on this page._"


def test_history_renders_interrupted_active_and_compacted_native_turns() -> None:
    text = render_thread_history(
        {
            "page": 2,
            "hasOlder": True,
            "turns": [
                {
                    "id": "turn_interrupted",
                    "status": "interrupted",
                    "error": {"message": "Quota reached"},
                    "items": [
                        {"type": "userMessage", "text": "Implement it"},
                        {"type": "contextCompaction"},
                    ],
                },
                {
                    "id": "turn_active",
                    "status": "inProgress",
                    "items": [{"type": "userMessage", "text": "Continue"}],
                },
            ],
        },
        limit=2,
    )

    assert "Thread History · Page 2" in text
    assert "Interrupted" in text
    assert "Working" in text
    assert "Quota reached" in text
    assert "Native context compaction occurred" in text
    assert "`/history 2 --page 3`" in text


def test_history_closes_a_truncated_code_fence_before_the_next_turn() -> None:
    text = render_thread_history(
        {
            "turns": [
                {
                    "id": "turn_1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "```python\n" + ("print('long output')\n" * 100) + "```",
                        }
                    ],
                },
                {"id": "turn_2", "status": "completed", "items": []},
            ]
        },
        limit=2,
    )

    separator = text.index("\n---\n")
    assert text[:separator].rstrip().endswith("```\n…")
    assert "### 2. Completed · `turn_2`" in text[separator:]

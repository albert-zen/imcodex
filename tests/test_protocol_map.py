from __future__ import annotations

from imcodex.appserver import normalize_appserver_message


def test_normalize_thread_name_updated_event() -> None:
    event = normalize_appserver_message(
        {
            "method": "thread/name/updated",
            "params": {
                "threadId": "thr_1",
                "name": "Investigate alpha",
            },
        }
    )

    assert event.kind == "thread_name_updated"
    assert event.thread_id == "thr_1"
    assert event.payload["name"] == "Investigate alpha"


def test_normalize_turn_diff_updated_event() -> None:
    event = normalize_appserver_message(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": "thr_1",
                "turnId": "turn_1",
                "summary": "Updated 2 files",
                "files": ["src/imcodex/bridge/core.py", "tests/test_service.py"],
            },
        }
    )

    assert event.kind == "diff_updated"
    assert event.thread_id == "thr_1"
    assert event.turn_id == "turn_1"
    assert event.payload["summary"] == "Updated 2 files"


def test_unknown_method_is_normalized_as_unknown_event() -> None:
    event = normalize_appserver_message(
        {
            "method": "future/unknown",
            "params": {
                "threadId": "thr_1",
            },
        }
    )

    assert event.kind == "unknown"
    assert event.thread_id == "thr_1"
    assert event.method == "future/unknown"

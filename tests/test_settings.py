from __future__ import annotations

from datetime import timedelta
from datetime import timezone

from imcodex.bridge.settings import _rate_limit_window_label
from imcodex.bridge.settings import render_credits


def test_rate_limit_window_label_shows_remaining_percent_and_local_reset_time() -> None:
    china_time = timezone(timedelta(hours=8), "CST")

    label = _rate_limit_window_label(
        "Primary",
        {
            "usedPercent": 2,
            "windowDurationMins": 300,
            "resetsAt": 1778436562,
        },
        tz=china_time,
    )

    assert label == "Primary: 98% remaining, window 300 min, resets at 2026-05-11 02:09:22 CST (UTC+08:00)"


def test_rate_limit_window_label_formats_fractional_remaining_percent() -> None:
    china_time = timezone(timedelta(hours=8), "CST")

    label = _rate_limit_window_label(
        "Secondary",
        {
            "usedPercent": "41.5",
            "windowDurationMins": 10080,
            "resetsAt": 1778732836,
        },
        tz=china_time,
    )

    assert label == "Secondary: 58.5% remaining, window 10080 min, resets at 2026-05-14 12:27:16 CST (UTC+08:00)"


def test_render_credits_uses_remaining_language() -> None:
    text = render_credits(
        {
            "rateLimits": {
                "limitId": "codex",
                "planType": "plus",
                "credits": {"hasCredits": False, "balance": 0},
                "primary": {"usedPercent": 2, "windowDurationMins": 300},
                "secondary": {"usedPercent": 41, "windowDurationMins": 10080},
            }
        }
    )

    assert text.startswith("Usage\n\n")
    assert "Plan: plus" in text
    assert "5h limit: 98% remaining" in text
    assert "Weekly limit: 59% remaining" in text
    assert "Credits: Depleted, balance 0" in text
    assert "Current:" not in text
    assert "Primary: 2%" not in text


def test_render_credits_falls_back_for_unusual_windows() -> None:
    text = render_credits(
        {
            "rateLimits": {
                "credits": {"unlimited": True},
                "primary": {"usedPercent": 10, "windowDurationMins": 60},
            }
        }
    )

    assert "Primary limit (60 min): 90% remaining" in text
    assert "Credits: Unlimited" in text

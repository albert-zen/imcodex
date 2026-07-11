from __future__ import annotations

from datetime import timedelta
from datetime import timezone

from imcodex.bridge.settings import _rate_limit_window_label
from imcodex.bridge.settings import current_model_label
from imcodex.bridge.settings import current_personality_label
from imcodex.bridge.settings import current_reasoning_label
from imcodex.bridge.settings import fast_mode_label
from imcodex.bridge.settings import permission_mode_label
from imcodex.bridge.settings import render_credits
from imcodex.bridge.settings import render_native_config_write_result
from imcodex.bridge.settings import render_permission_modes
from imcodex.bridge.settings import render_permission_set_result
from imcodex.bridge.settings import render_personality
from imcodex.bridge.settings import render_reasoning_effort


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


def test_render_credits_combines_rate_limits_and_usage() -> None:
    text = render_credits(
        {
            "rateLimitsResult": {
                "rateLimits": {
                    "planType": "pro",
                    "credits": {"hasCredits": True, "balance": "123"},
                    "primary": {"usedPercent": 25, "windowDurationMins": 300},
                }
            },
            "usageResult": {
                "summary": {
                    "lifetimeTokens": 6007921192,
                    "peakDailyTokens": 504382843,
                    "longestRunningTurnSec": 8943,
                    "currentStreakDays": 33,
                    "longestStreakDays": 40,
                },
                "dailyUsageBuckets": [
                    {"startDate": "2026-06-25", "tokens": 294319854},
                    {"startDate": "2026-06-26", "tokens": 2314249},
                ],
            },
        }
    )

    assert "Plan: pro" in text
    assert "Credits: Available, balance 123" in text
    assert "Tokens: 6B lifetime, 504.4M peak/day" in text
    assert "Streak: 33 days current, 40 days longest" in text
    assert "Longest turn: 2h 29m" in text
    assert "Latest day: 2026-06-26 2.3M tokens" in text


def test_render_credits_shows_partial_warning() -> None:
    text = render_credits(
        {
            "usageResult": {
                "summary": {"lifetimeTokens": 1234},
                "dailyUsageBuckets": [],
            },
            "warnings": {"rateLimits": "method failed"},
        }
    )

    assert "Tokens: 1.2K lifetime" in text
    assert "Warning: credits and rate limits could not be queried from Codex right now." in text


def test_render_permission_modes_uses_native_profiles_and_requirements() -> None:
    text = render_permission_modes(
        {
            "config": {"default_permissions": ":danger-full-access"},
            "profiles": [
                {"id": ":read-only", "description": "Only reads files"},
                {"id": ":workspace"},
                {"id": ":danger-full-access"},
                {"id": "team/custom", "description": "Team profile"},
            ],
            "requirements": {
                "allowedPermissionProfiles": {
                    ":read-only": True,
                    ":workspace": True,
                    ":danger-full-access": False,
                    "team/custom": True,
                }
            },
        }
    )

    assert "Current: Full Access" in text
    assert "Native profiles:" in text
    assert "- :read-only: Only reads files" in text
    assert "- team/custom: Team profile" in text
    assert "- /permission default (:workspace)" in text
    assert "- /permission read-only (:read-only)" in text
    assert "Unavailable by Codex requirements:" in text
    assert "- /permission full-access (:danger-full-access)" in text


def test_settings_labels_accept_native_camel_case_effective_config() -> None:
    config = {
        "modelId": "gpt-5.4",
        "reasoningEffort": "high",
        "serviceTier": "fast",
        "features": {"fastMode": True},
        "permissionProfile": ":read-only",
        "approvalPolicy": "on-request",
        "sandbox": {"mode": "read-only"},
    }

    assert current_model_label(config) == "gpt-5.4"
    assert current_reasoning_label(config) == "high"
    assert fast_mode_label(config) == "Fast"
    assert permission_mode_label(config) == "Read Only"


def test_render_reasoning_effort_uses_native_model_catalog_options() -> None:
    text = render_reasoning_effort(
        {
            "config": {"model_reasoning_effort": "high"},
            "selectedModel": "gpt-5.5",
            "selectedModelDisplayName": "GPT-5.5",
            "defaultReasoningEffort": "medium",
            "reasoningOptionsSource": "native",
            "reasoningEfforts": [
                {"reasoningEffort": "low", "description": "Faster answers"},
                {"reasoningEffort": "medium", "description": "Balanced"},
                {"reasoningEffort": "ultra", "description": "Deepest reasoning"},
            ],
        }
    )

    assert "Current: high" in text
    assert "Model: GPT-5.5" in text
    assert "/think low: Faster answers" in text
    assert "/think medium: Balanced; model default" in text
    assert "/think ultra: Deepest reasoning" in text
    assert "/think minimal" not in text


def test_render_personality_defaults_to_native_default_and_lists_choices() -> None:
    text = render_personality({"config": {"personality": None}})

    assert "Current: Default" in text
    assert "/personality default" in text
    assert "/personality none" in text
    assert current_personality_label({"personality": "pragmatic"}) == "Pragmatic"


def test_permission_mode_label_falls_back_to_legacy_config() -> None:
    assert permission_mode_label({"approval_policy": "on-request", "sandbox_mode": "workspace-write"}) == "Default"


def test_permission_profile_definitions_are_not_rendered_as_the_selected_profile() -> None:
    assert permission_mode_label({"permissions": {"team": {"sandbox": "read-only"}}}) == "Custom"


def test_render_permission_set_result_marks_compatibility_fallback() -> None:
    text = render_permission_set_result({"mode": "read-only", "fallback": True})

    assert "Native permission preference set to Read Only." in text
    assert "new or cold-loaded threads" in text
    assert "compatibility config" in text


def test_native_config_write_renderers_do_not_claim_overridden_values_are_effective() -> None:
    payload = {"status": "okOverridden", "overriddenMetadata": {"source": "commandLine"}}

    personality = render_native_config_write_result(
        payload,
        "Native personality preference set to pragmatic.",
        setting_label="Personality",
    )
    permission = render_permission_set_result({**payload, "mode": "full-access"})

    assert personality == (
        "Personality preference was saved, but a higher-priority native Codex configuration remains effective."
    )
    assert permission == (
        "Permission preference Full Access was saved, but a higher-priority native Codex configuration remains effective."
    )

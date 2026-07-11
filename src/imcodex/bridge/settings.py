from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any


_PERMISSION_PROFILE_IDS = {
    "default": ":workspace",
    "read-only": ":read-only",
    "full-access": ":danger-full-access",
}
_PERMISSION_PROFILE_LABELS = {
    ":workspace": "Default",
    ":read-only": "Read Only",
    ":danger-full-access": "Full Access",
}
_PERMISSION_MODE_LABELS = {
    "default": "Default",
    "read-only": "Read Only",
    "full-access": "Full Access",
}


def render_models(payload: dict) -> str:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        return "Models\n\nCurrent: Unknown"
    current = next((item for item in items if isinstance(item, dict) and item.get("isDefault")), None)
    current_label = model_label(current) if isinstance(current, dict) else "Unknown"
    lines = ["Models", "", f"Current: {current_label}", "", "Available:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(f"- {model_label(item)}")
    lines.append("")
    lines.append("Use /model <model-id> to switch directly.")
    return "\n".join(lines)


def render_reasoning_effort(payload: dict) -> str:
    config = effective_config(payload)
    current = current_reasoning_label(config)
    lines = ["Reasoning Effort", "", f"Current: {current}"]
    model = str(payload.get("selectedModelDisplayName") or payload.get("selectedModel") or "").strip()
    if model:
        lines.append(f"Model: {model}")
    lines.extend(["", "Available:"])
    default_effort = str(payload.get("defaultReasoningEffort") or "").strip().lower()
    efforts = payload.get("reasoningEfforts")
    if isinstance(efforts, list) and efforts:
        for item in efforts:
            if not isinstance(item, dict):
                continue
            effort = str(item.get("reasoningEffort") or "").strip()
            if not effort:
                continue
            details: list[str] = []
            description = str(item.get("description") or "").strip()
            if description:
                details.append(description)
            if effort.lower() == default_effort:
                details.append("model default")
            suffix = f": {'; '.join(details)}" if details else ""
            lines.append(f"- /think {effort}{suffix}")
    else:
        lines.append("- No configurable effort advertised by this model")
    lines.append("- /think default")
    if payload.get("reasoningOptionsSource") == "fallback":
        lines.extend(["", "Using compatibility effort choices because the native model metadata was unavailable."])
    lines.extend(["", "This is the configured default; an already-loaded thread may retain its native settings."])
    return "\n".join(lines)


def render_personality(payload: dict) -> str:
    config = effective_config(payload)
    current = current_personality_label(config)
    return "\n".join(
        [
            "Personality",
            "",
            f"Current: {current}",
            "",
            "- /personality default",
            "- /personality none",
            "- /personality friendly",
            "- /personality pragmatic",
            "",
            "This is the configured default; an already-loaded thread may retain its native settings.",
        ]
    )


def render_fast_mode(payload: dict) -> str:
    config = effective_config(payload)
    current = fast_mode_label(config)
    return "\n".join(
        [
            "Fast Mode",
            "",
            f"Current: {current}",
            "",
            "- /fast on",
            "- /fast off",
            "- /fast status",
        ]
    )


def render_credits(payload: dict) -> str:
    rate_limits_payload = payload.get("rateLimitsResult") if isinstance(payload.get("rateLimitsResult"), dict) else payload
    usage_payload = payload.get("usageResult") if isinstance(payload.get("usageResult"), dict) else None
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), dict) else {}
    rate_limits = _rate_limit_snapshot(rate_limits_payload)
    if rate_limits is None and usage_payload is None:
        return "Usage\n\nPlan: Unknown\nCredits: Unknown"

    lines = ["Usage", ""]
    if rate_limits is None:
        lines.append("Credits and rate limits: Unavailable")
    else:
        credits = rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else None
        if rate_limits.get("planType"):
            lines.append(f"Plan: {rate_limits['planType']}")
        reached_type = rate_limits.get("rateLimitReachedType")
        if reached_type:
            lines.append(f"Limit state: {reached_type}")
        for fallback_label, key in (("Primary limit", "primary"), ("Secondary limit", "secondary")):
            window = rate_limits.get(key)
            if isinstance(window, dict):
                lines.append(_usage_rate_limit_window_label(fallback_label, window))
        lines.append(_credits_line(credits))
    usage_lines = _account_usage_lines(usage_payload)
    if usage_lines:
        if len(lines) > 2:
            lines.append("")
        lines.extend(usage_lines)
    warning_lines = _credit_warning_lines(warnings)
    if warning_lines:
        lines.append("")
        lines.extend(warning_lines)
    return "\n".join(lines)


def render_permission_modes(payload: dict) -> str:
    config = effective_config(payload)
    current = permission_mode_label(config)
    lines = ["Permission Modes", "", f"Current: {current}", ""]
    profiles = _permission_profile_items(payload.get("profiles"))
    if profiles:
        requirements = payload.get("requirements")
        blocked_profile_ids = _blocked_permission_profile_ids(profiles, requirements)
        lines.append("Native profiles:")
        for profile in profiles:
            profile_id = str(profile.get("id") or "")
            description = str(profile.get("description") or "").strip()
            details: list[str] = []
            if description:
                details.append(description)
            if profile_id in blocked_profile_ids:
                details.append("blocked by Codex requirements")
            suffix = f": {'; '.join(details)}" if details else ""
            lines.append(f"- {profile_id}{suffix}")
        lines.append("")
        lines.append("Shortcuts:")
        allowed_modes = _allowed_permission_modes(profiles, requirements)
        for mode in ("default", "read-only", "full-access"):
            if mode in allowed_modes:
                lines.append(f"- /permission {mode} ({_PERMISSION_PROFILE_IDS[mode]})")
        blocked_modes = [mode for mode in ("default", "read-only", "full-access") if mode not in allowed_modes]
        if blocked_modes:
            lines.append("")
            lines.append("Unavailable by Codex requirements:")
            lines.extend(f"- /permission {mode} ({_PERMISSION_PROFILE_IDS[mode]})" for mode in blocked_modes)
    else:
        lines.extend(
            [
                "- /permission default",
                "- /permission read-only",
                "- /permission full-access",
            ]
        )
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), dict) else {}
    if payload.get("nativeProfilesSupported") is False:
        lines.append("")
        lines.append("Using compatibility permission settings for this Codex version.")
    elif "requirements" in warnings:
        lines.append("")
        lines.append("Codex requirements could not be read; showing available profiles only.")
    return "\n".join(lines)


def render_permission_set_result(payload: dict) -> str:
    mode = str(payload.get("mode") or "").strip()
    label = _PERMISSION_MODE_LABELS.get(mode, mode or "requested mode")
    if native_config_write_was_overridden(payload):
        return (
            f"Permission preference {label} was saved, but a higher-priority native Codex configuration "
            "remains effective."
        )
    lines = [f"Native permission preference set to {label}."]
    lines.append("It applies to new or cold-loaded threads; already-loaded threads keep their native settings.")
    if payload.get("fallback"):
        lines.append("Used compatibility config because native permission profiles are unavailable.")
    return "\n".join(lines)


def render_native_config_write_result(payload: dict, success_text: str, *, setting_label: str) -> str:
    if native_config_write_was_overridden(payload):
        return (
            f"{setting_label} preference was saved, but a higher-priority native Codex configuration "
            "remains effective."
        )
    return success_text


def native_config_write_was_overridden(payload: dict) -> bool:
    status = str(payload.get("status") or "").strip().lower().replace("_", "").replace("-", "")
    return "overridden" in status or payload.get("overriddenMetadata") is not None


def model_label(item: dict) -> str:
    display = str(item.get("displayName") or item.get("model") or item.get("id") or "unknown")
    model_id = str(item.get("model") or item.get("id") or display)
    if display == model_id:
        return display
    return f"{display} ({model_id})"


def current_model_label(config: dict) -> str:
    model = _first_config_value(config, "model", "modelId", "modelID")
    if not model:
        return "Default"
    return str(model)


def current_reasoning_label(config: dict) -> str:
    effort = _first_config_value(config, "model_reasoning_effort", "reasoningEffort")
    if not effort:
        return "Default"
    return str(effort)


def current_personality_label(config: dict) -> str:
    personality = _first_config_value(config, "personality")
    if not personality:
        return "Default"
    labels = {
        "none": "None",
        "friendly": "Friendly",
        "pragmatic": "Pragmatic",
    }
    value = str(personality)
    return labels.get(value.lower(), value)


def fast_mode_label(config: dict) -> str:
    tier = str(_first_config_value(config, "service_tier", "serviceTier") or "").strip().lower()
    features = _first_config_value(config, "features")
    features = features if isinstance(features, dict) else {}
    fast_enabled = features.get("fast_mode") if isinstance(features, dict) else None
    if fast_enabled is None and isinstance(features, dict):
        fast_enabled = features.get("fastMode")
    if tier == "fast" and fast_enabled is True:
        return "Fast"
    if tier in {"", "standard"} and fast_enabled in {None, False}:
        return "Standard"
    details = []
    if tier:
        details.append(f"service_tier={tier}")
    if fast_enabled is not None:
        details.append(f"fast_mode={str(fast_enabled).lower()}")
    return f"Custom ({', '.join(details)})" if details else "Standard"


def permission_mode_label(config: dict) -> str:
    profile = _first_config_value(config, "default_permissions", "permissionProfile")
    if profile:
        return _permission_profile_label(str(profile))
    active_profile = config.get("activePermissionProfile") or config.get("active_permission_profile")
    if isinstance(active_profile, dict) and active_profile.get("id"):
        return _permission_profile_label(str(active_profile["id"]))
    approval = str(_first_config_value(config, "approval_policy", "approvalPolicy") or "")
    sandbox_value = _first_config_value(config, "sandbox_mode", "sandboxMode", "sandbox")
    if isinstance(sandbox_value, dict):
        sandbox = str(sandbox_value.get("mode") or sandbox_value.get("type") or "")
    else:
        sandbox = str(sandbox_value or "")
    if approval == "on-request" and sandbox == "workspace-write":
        return "Default"
    if approval == "on-request" and sandbox == "read-only":
        return "Read Only"
    if approval == "never" and sandbox == "danger-full-access":
        return "Full Access"
    details = ", ".join(part for part in (approval, sandbox) if part)
    return f"Custom ({details})" if details else "Custom"


def effective_config(payload: dict) -> dict:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else None
    if config is not None:
        return config
    return payload if isinstance(payload, dict) else {}


def _first_config_value(config: dict, *keys: str) -> object | None:
    for key in keys:
        if key in config and config.get(key) is not None:
            return config[key]
    return None


def _account_usage_lines(payload: dict | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines: list[str] = []
    token_parts: list[str] = []
    lifetime = _int_or_none(summary.get("lifetimeTokens"))
    if lifetime is not None:
        token_parts.append(f"{_format_compact_number(lifetime)} lifetime")
    peak_daily = _int_or_none(summary.get("peakDailyTokens"))
    if peak_daily is not None:
        token_parts.append(f"{_format_compact_number(peak_daily)} peak/day")
    if token_parts:
        lines.append("Tokens: " + ", ".join(token_parts))
    current_streak = _int_or_none(summary.get("currentStreakDays"))
    longest_streak = _int_or_none(summary.get("longestStreakDays"))
    streak_parts: list[str] = []
    if current_streak is not None:
        streak_parts.append(f"{_format_day_count(current_streak)} current")
    if longest_streak is not None:
        streak_parts.append(f"{_format_day_count(longest_streak)} longest")
    if streak_parts:
        lines.append("Streak: " + ", ".join(streak_parts))
    longest_turn = _int_or_none(summary.get("longestRunningTurnSec"))
    if longest_turn is not None:
        lines.append(f"Longest turn: {_format_duration_seconds(longest_turn)}")
    latest = _latest_daily_usage_bucket(payload.get("dailyUsageBuckets"))
    if latest is not None:
        date, tokens = latest
        lines.append(f"Latest day: {date} {_format_compact_number(tokens)} tokens")
    return lines


def _credit_warning_lines(warnings: dict) -> list[str]:
    lines: list[str] = []
    if "rateLimits" in warnings:
        lines.append("Warning: credits and rate limits could not be queried from Codex right now.")
    if "usage" in warnings:
        lines.append("Warning: usage could not be queried from Codex right now.")
    return lines


def _permission_profile_items(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("id")]


def _allowed_permission_modes(profiles: list[dict], requirements: object) -> set[str]:
    profile_ids = {str(profile.get("id")) for profile in profiles}
    allowed = _allowed_permission_profile_map(requirements)
    modes: set[str] = set()
    for mode, profile_id in _PERMISSION_PROFILE_IDS.items():
        if profile_id not in profile_ids:
            continue
        if allowed is not None and not bool(allowed.get(profile_id)):
            continue
        modes.add(mode)
    return modes


def _blocked_permission_profile_ids(profiles: list[dict], requirements: object) -> set[str]:
    allowed = _allowed_permission_profile_map(requirements)
    if allowed is None:
        return set()
    return {
        str(profile.get("id"))
        for profile in profiles
        if profile.get("id") is not None and not bool(allowed.get(str(profile.get("id"))))
    }


def _allowed_permission_profile_map(requirements: object) -> dict | None:
    if isinstance(requirements, dict) and isinstance(requirements.get("allowedPermissionProfiles"), dict):
        return requirements["allowedPermissionProfiles"]
    return None


def _permission_profile_label(profile_id: str) -> str:
    if profile_id in _PERMISSION_PROFILE_LABELS:
        return _PERMISSION_PROFILE_LABELS[profile_id]
    return f"Profile {profile_id}"


def _rate_limit_snapshot(payload: dict) -> dict | None:
    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        codex = by_limit_id.get("codex")
        if isinstance(codex, dict):
            return codex
        for snapshot in by_limit_id.values():
            if isinstance(snapshot, dict):
                return snapshot
    rate_limits = payload.get("rateLimits")
    return rate_limits if isinstance(rate_limits, dict) else None


def _rate_limit_window_label(label: str, window: dict, *, tz=None) -> str:
    remaining = _remaining_percent(window.get("usedPercent"))
    parts = [f"{label}: {remaining} remaining" if remaining is not None else f"{label}: unknown"]
    duration = window.get("windowDurationMins")
    if duration is not None:
        parts.append(f"window {duration} min")
    resets_at = window.get("resetsAt")
    if resets_at is not None:
        parts.append(f"resets at {_format_reset_time(resets_at, tz=tz)}")
    return ", ".join(parts)


def _usage_rate_limit_window_label(label: str, window: dict) -> str:
    display_label = _rate_limit_window_display_label(label, window.get("windowDurationMins"))
    remaining = _remaining_percent(window.get("usedPercent")) or "unknown"
    parts = [f"{display_label}: {remaining} remaining"]
    resets_at = window.get("resetsAt")
    if resets_at is not None:
        parts.append(f"resets {_format_reset_time(resets_at)}")
    return ", ".join(parts)


def _rate_limit_window_display_label(fallback_label: str, duration_mins: Any) -> str:
    try:
        minutes = int(duration_mins)
    except (TypeError, ValueError):
        return fallback_label
    if minutes == 300:
        return "5h limit"
    if minutes == 10080:
        return "Weekly limit"
    return f"{fallback_label} ({minutes} min)"


def _credits_line(credits: dict | None) -> str:
    if not isinstance(credits, dict):
        return "Credits: Unknown"
    if credits.get("unlimited") is True:
        status = "Unlimited"
    elif credits.get("hasCredits") is True:
        status = "Available"
    elif credits.get("hasCredits") is False:
        status = "Depleted"
    else:
        status = "Unknown"
    if credits.get("balance") is not None:
        return f"Credits: {status}, balance {credits['balance']}"
    return f"Credits: {status}"


def _remaining_percent(used_percent: Any) -> str | None:
    if used_percent is None:
        return None
    try:
        used = float(str(used_percent).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    remaining = max(0.0, min(100.0, 100.0 - used))
    if remaining.is_integer():
        return f"{int(remaining)}%"
    return f"{remaining:.1f}".rstrip("0").rstrip(".") + "%"


def _latest_daily_usage_bucket(value: object) -> tuple[str, int] | None:
    if not isinstance(value, list):
        return None
    buckets: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        date = str(item.get("startDate") or "").strip()
        tokens = _int_or_none(item.get("tokens"))
        if date and tokens is not None:
            buckets.append((date, tokens))
    if not buckets:
        return None
    buckets.sort(key=lambda item: item[0])
    return buckets[-1]


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_compact_number(value: int) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B".replace(".0B", "B")
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def _format_duration_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours >= 24:
        days = hours // 24
        remaining_hours = hours % 24
        return f"{days}d {remaining_hours}h {remaining_minutes}m"
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def _format_day_count(days: int) -> str:
    suffix = "" if days == 1 else "s"
    return f"{days} day{suffix}"


def _format_reset_time(value: Any, *, tz=None) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    target_tz = tz
    if target_tz is None:
        target_tz = datetime.now().astimezone().tzinfo or timezone.utc
    reset = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(target_tz)
    offset = _utc_offset_label(reset)
    zone_name = reset.tzname()
    if zone_name and zone_name != offset:
        zone_label = f"{zone_name} ({offset})"
    else:
        zone_label = offset
    return f"{reset:%Y-%m-%d %H:%M:%S} {zone_label}"


def _utc_offset_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return "UTC"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"

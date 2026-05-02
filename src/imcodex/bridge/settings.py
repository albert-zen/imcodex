from __future__ import annotations


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
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    current = current_reasoning_label(config)
    return "\n".join(
        [
            "Reasoning Effort",
            "",
            f"Current: {current}",
            "",
            "- /think minimal",
            "- /think low",
            "- /think medium",
            "- /think high",
            "- /think xhigh",
            "- /think default",
        ]
    )


def render_fast_mode(payload: dict) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
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
    rate_limits = _rate_limit_snapshot(payload)
    if rate_limits is None:
        return "Credits\n\nCurrent: Unknown"

    credits = rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else None
    lines = ["Credits", ""]
    if credits is None:
        lines.append("Current: Unknown")
    elif credits.get("unlimited") is True:
        lines.append("Current: Unlimited")
    elif credits.get("hasCredits") is True:
        lines.append("Current: Available")
    elif credits.get("hasCredits") is False:
        lines.append("Current: Depleted")
    else:
        lines.append("Current: Unknown")

    if isinstance(credits, dict) and credits.get("balance") is not None:
        lines.append(f"Balance: {credits['balance']}")
    if rate_limits.get("planType"):
        lines.append(f"Plan: {rate_limits['planType']}")
    if rate_limits.get("limitName") or rate_limits.get("limitId"):
        lines.append(f"Limit: {rate_limits.get('limitName') or rate_limits.get('limitId')}")
    reached_type = rate_limits.get("rateLimitReachedType")
    if reached_type:
        lines.append(f"Limit state: {reached_type}")
    for label, key in (("Primary", "primary"), ("Secondary", "secondary")):
        window = rate_limits.get(key)
        if isinstance(window, dict):
            lines.append(_rate_limit_window_label(label, window))
    return "\n".join(lines)


def render_permission_modes(payload: dict) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    current = permission_mode_label(config)
    return "\n".join(
        [
            "Permission Modes",
            "",
            f"Current: {current}",
            "",
            "- /permission default",
            "- /permission read-only",
            "- /permission full-access",
        ]
    )


def model_label(item: dict) -> str:
    display = str(item.get("displayName") or item.get("model") or item.get("id") or "unknown")
    model_id = str(item.get("model") or item.get("id") or display)
    if display == model_id:
        return display
    return f"{display} ({model_id})"


def current_model_label(config: dict) -> str:
    model = config.get("model")
    if not model:
        return "Default"
    return str(model)


def current_reasoning_label(config: dict) -> str:
    effort = config.get("model_reasoning_effort")
    if not effort:
        return "Default"
    return str(effort)


def fast_mode_label(config: dict) -> str:
    tier = str(config.get("service_tier") or "").strip().lower()
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    fast_enabled = features.get("fast_mode") if isinstance(features, dict) else None
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
    approval = str(config.get("approval_policy") or "")
    sandbox = str(config.get("sandbox_mode") or "")
    if approval == "on-request" and sandbox == "workspace-write":
        return "Default"
    if approval == "on-request" and sandbox == "read-only":
        return "Read Only"
    if approval == "never" and sandbox == "danger-full-access":
        return "Full Access"
    details = ", ".join(part for part in (approval, sandbox) if part)
    return f"Custom ({details})" if details else "Custom"


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


def _rate_limit_window_label(label: str, window: dict) -> str:
    used = window.get("usedPercent")
    parts = [f"{label}: {used}%" if used is not None else f"{label}: unknown"]
    duration = window.get("windowDurationMins")
    if duration is not None:
        parts.append(f"window {duration} min")
    resets_at = window.get("resetsAt")
    if resets_at is not None:
        parts.append(f"resets at {resets_at}")
    return ", ".join(parts)

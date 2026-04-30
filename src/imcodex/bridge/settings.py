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

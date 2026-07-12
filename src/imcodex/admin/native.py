from __future__ import annotations

from typing import Any


_PERSONALITIES = ("default", "none", "friendly", "pragmatic")
_PERMISSION_PROFILES = {
    "default": ":workspace",
    "read-only": ":read-only",
    "full-access": ":danger-full-access",
}
_PERMISSION_LABELS = {
    "default": "Default",
    "read-only": "Read only",
    "full-access": "Full access",
}
_PERMISSION_DESCRIPTIONS = {
    "default": "Workspace access with approval when Codex needs it.",
    "read-only": "Read-only access with approval when Codex needs it.",
    "full-access": "Full computer access without approval prompts.",
}
_PERMISSION_APPROVALS = {
    "default": "on-request",
    "read-only": "on-request",
    "full-access": "never",
}


def public_native_settings(payload: dict, *, csrf_token: str) -> dict[str, Any]:
    """Project only the native settings the console is allowed to expose."""

    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    effective_config = (
        payload.get("effectiveGlobalConfig")
        if isinstance(payload.get("effectiveGlobalConfig"), dict)
        else config
    )
    selected_model_id = _string_or_none(payload.get("selectedModel")) or _string_or_none(
        _first(effective_config, "model", "modelId", "modelID")
    )
    models = _public_models(payload.get("models"), selected_model=selected_model_id)
    selected_model = _selected_model(models, selected_model_id)
    reasoning_efforts = _public_reasoning_efforts(payload.get("reasoningEfforts"))
    profiles = [item for item in payload.get("profiles", []) if isinstance(item, dict)]
    requirements = payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {}

    managed_settings = _public_setting_names(payload.get("managedSettings"))
    fast_available = (
        payload.get("fastAvailable")
        if isinstance(payload.get("fastAvailable"), bool)
        else _fast_feature_available(config, requirements)
    )
    personality_available = (
        payload.get("personalityAvailable") is not False
        and (selected_model or {}).get("supportsPersonality") is not False
    )

    return {
        "available": True,
        "csrfToken": csrf_token,
        "config": {
            "model": _string_or_none(_first(effective_config, "model", "modelId", "modelID")),
            "reasoningEffort": _string_or_none(
                _first(effective_config, "model_reasoning_effort", "reasoningEffort")
            ),
            "personality": _string_or_none(_first(effective_config, "personality")) or "default",
            "fast": _fast_enabled(
                effective_config,
                selected_model=selected_model,
                feature_available=fast_available,
            ),
            "permissionMode": _permission_mode(effective_config),
        },
        "models": models,
        "reasoningEfforts": reasoning_efforts,
        "defaultReasoningEffort": _string_or_none(payload.get("defaultReasoningEffort")),
        "selectedModel": selected_model_id,
        "managedSettings": managed_settings,
        "readOnlySettings": managed_settings,
        "fastAvailable": fast_available,
        "personalityAvailable": personality_available,
        "personalityOptions": list(_PERSONALITIES),
        "permissionModes": _permission_modes(
            profiles,
            requirements,
            native_profiles_supported=payload.get("nativeProfilesSupported") is not False,
        ),
        "warnings": _public_warnings(payload),
    }


async def apply_global_setting(backend, *, setting: str, value: object) -> dict:
    if setting == "preferences":
        if not isinstance(value, dict) or not value or not set(value).issubset(
            {"model", "reasoningEffort", "personality", "fast"}
        ):
            raise ValueError("preferences must contain only model, reasoningEffort, personality, and fast")
        normalized: dict[str, object] = {}
        if "model" in value:
            normalized["model"] = _optional_text(value["model"], label="model", max_chars=200)
        if "reasoningEffort" in value:
            normalized["reasoningEffort"] = _optional_text(
                value["reasoningEffort"],
                label="reasoning effort",
                max_chars=32,
            )
        if "personality" in value:
            personality = _optional_text(value["personality"], label="personality", max_chars=32) or "default"
            if personality not in _PERSONALITIES:
                raise ValueError("personality must be default, none, friendly, or pragmatic")
            normalized["personality"] = None if personality == "default" else personality
        if "fast" in value:
            if not isinstance(value["fast"], bool):
                raise ValueError("fast must be a boolean")
            normalized["fast"] = value["fast"]
        return await backend.set_global_preferences(normalized)
    if setting == "model":
        normalized = _optional_text(value, label="model", max_chars=200)
        return await backend.set_global_model(normalized)
    if setting == "reasoningEffort":
        normalized = _optional_text(value, label="reasoning effort", max_chars=32)
        return await backend.set_global_reasoning_effort(normalized)
    if setting == "personality":
        normalized = _optional_text(value, label="personality", max_chars=32) or "default"
        if normalized not in _PERSONALITIES:
            raise ValueError("personality must be default, none, friendly, or pragmatic")
        return await backend.set_global_personality(None if normalized == "default" else normalized)
    if setting == "fast":
        if not isinstance(value, bool):
            raise ValueError("fast must be a boolean")
        return await backend.set_global_fast_mode(value)
    if setting == "permissionMode":
        normalized = _optional_text(value, label="permission mode", max_chars=32)
        if normalized not in _PERMISSION_PROFILES:
            raise ValueError("permissionMode must be default, read-only, or full-access")
        return await backend.set_global_permission_mode(normalized)
    raise ValueError(f"unsupported native setting: {setting}")


def public_write_result(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "updated"}
    allowed = ("status", "mode", "profile", "fallback", "changed", "reason", "warning")
    return {key: _public_scalar(result[key]) for key in allowed if key in result and _is_public_scalar(result[key])}


def _public_models(value: object, *, selected_model: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    models: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        model_id = _string_or_none(item.get("id") or item.get("model"))
        if not model_id:
            continue
        if item.get("hidden") is True and model_id != selected_model:
            continue
        model = {
            "id": model_id,
            "displayName": _string_or_none(item.get("displayName")) or model_id,
            "isDefault": item.get("isDefault") is True,
            "defaultReasoningEffort": _string_or_none(item.get("defaultReasoningEffort")),
            "defaultServiceTier": _string_or_none(item.get("defaultServiceTier")),
            "serviceTiers": _public_service_tiers(item.get("serviceTiers")),
            "additionalSpeedTiers": _public_string_list(item.get("additionalSpeedTiers")),
        }
        if isinstance(item.get("supportedReasoningEfforts"), list):
            model["supportedReasoningEfforts"] = _public_reasoning_efforts(item["supportedReasoningEfforts"])
        if isinstance(item.get("supportsPersonality"), bool):
            model["supportsPersonality"] = item["supportsPersonality"]
        models.append(model)
    return models


def _selected_model(models: list[dict[str, Any]], selected_model: str | None) -> dict[str, Any] | None:
    if selected_model is not None:
        selected = next((model for model in models if model.get("id") == selected_model), None)
        if selected is not None:
            return selected
    return next((model for model in models if model.get("isDefault") is True), None)


def _public_setting_names(value: object) -> list[str]:
    allowed = {"model", "reasoningEffort", "personality", "permissionMode", "fast"}
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in allowed))


def _public_service_tiers(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    tiers: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        tier_id = _string_or_none(item.get("id"))
        if not tier_id or tier_id in seen:
            continue
        seen.add(tier_id)
        entry = {
            "id": tier_id,
            "name": _string_or_none(item.get("name")) or tier_id,
        }
        description = _public_text(item.get("description"))
        if description:
            entry["description"] = description
        tiers.append(entry)
    return tiers


def _public_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _string_or_none(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _public_reasoning_efforts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    efforts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            effort = item.strip().lower()
            description = ""
        elif isinstance(item, dict):
            effort = (_string_or_none(item.get("reasoningEffort") or item.get("id")) or "").lower()
            description = _public_text(item.get("description")) or ""
        else:
            continue
        if not effort or effort in seen:
            continue
        seen.add(effort)
        entry = {"reasoningEffort": effort}
        if description:
            entry["description"] = description[:500]
        efforts.append(entry)
    return efforts


def _permission_modes(
    profiles: list[dict],
    requirements: dict,
    *,
    native_profiles_supported: bool,
) -> list[dict[str, Any]]:
    profiles_by_id = {profile_id: item for item in profiles if (profile_id := _string_or_none(item.get("id")))}
    allowed_profiles = requirements.get("allowedPermissionProfiles")
    allowed_approvals = requirements.get("allowedApprovalPolicies")
    allowed_sandboxes = requirements.get("allowedSandboxModes")
    approval_for_mode = {
        "default": "on-request",
        "read-only": "on-request",
        "full-access": "never",
    }
    sandbox_for_mode = {
        "default": "workspace-write",
        "read-only": "read-only",
        "full-access": "danger-full-access",
    }
    modes: list[dict[str, Any]] = []
    for mode, profile_id in _PERMISSION_PROFILES.items():
        profile = profiles_by_id.get(profile_id)
        available = True
        if native_profiles_supported:
            available = profile is not None and profile.get("allowed") is not False
        if isinstance(allowed_profiles, dict):
            available = available and bool(allowed_profiles.get(profile_id))
        if isinstance(allowed_approvals, list):
            available = available and approval_for_mode[mode] in allowed_approvals
        if not native_profiles_supported and isinstance(allowed_sandboxes, list):
            available = available and sandbox_for_mode[mode] in allowed_sandboxes
        description = _PERMISSION_DESCRIPTIONS[mode]
        native_description = _public_text((profile or {}).get("description"))
        if native_description:
            description = f"{description} Codex: {native_description}"
        entry: dict[str, Any] = {
            "id": mode,
            "label": _PERMISSION_LABELS[mode],
            "profileId": profile_id,
            "available": available,
        }
        if description:
            entry["description"] = description[:500]
        modes.append(entry)
    return modes


def _permission_mode(config: dict) -> str:
    profile = _string_or_none(_first(config, "default_permissions", "permissionProfile"))
    for mode, profile_id in _PERMISSION_PROFILES.items():
        approval = _string_or_none(_first(config, "approval_policy", "approvalPolicy")) or "on-request"
        if profile == profile_id and approval == _PERMISSION_APPROVALS[mode]:
            return mode
    approval = _string_or_none(_first(config, "approval_policy", "approvalPolicy"))
    sandbox_value = _first(config, "sandbox_mode", "sandboxMode", "sandbox")
    if isinstance(sandbox_value, dict):
        sandbox = _string_or_none(sandbox_value.get("mode") or sandbox_value.get("type"))
    else:
        sandbox = _string_or_none(sandbox_value)
    legacy = {
        ("on-request", "workspace-write"): "default",
        ("on-request", "read-only"): "read-only",
        ("never", "danger-full-access"): "full-access",
    }
    return legacy.get((approval, sandbox), "custom")


def _fast_enabled(
    config: dict,
    *,
    selected_model: dict[str, Any] | None = None,
    feature_available: bool = True,
) -> bool:
    if not feature_available:
        return False
    configured_tier = _first(config, "service_tier", "serviceTier")
    if configured_tier is not None:
        configured_fast = str(configured_tier).strip().lower() in {"priority", "fast"}
        return configured_fast and (
            selected_model is None or _model_supports_fast_tier(selected_model)
        )
    default_tier = (selected_model or {}).get("defaultServiceTier")
    return str(default_tier or "").strip().lower() in {"priority", "fast"}


def _model_supports_fast_tier(model: dict[str, Any]) -> bool:
    default_tier = str(model.get("defaultServiceTier") or "").strip().lower()
    if default_tier in {"priority", "fast"}:
        return True
    service_tiers = model.get("serviceTiers")
    if isinstance(service_tiers, list) and any(
        isinstance(tier, dict) and str(tier.get("id") or "").strip().lower() in {"priority", "fast"}
        for tier in service_tiers
    ):
        return True
    additional = model.get("additionalSpeedTiers")
    return isinstance(additional, list) and any(
        str(tier).strip().lower() in {"priority", "fast"} for tier in additional
    )


def _fast_feature_available(config: dict, requirements: dict) -> bool:
    features = config.get("features")
    if isinstance(features, dict) and any(
        features.get(key) is False for key in ("fast_mode", "fastMode") if key in features
    ):
        return False
    feature_requirements = requirements.get("featureRequirements")
    if not isinstance(feature_requirements, dict):
        feature_requirements = requirements.get("feature_requirements")
    if not isinstance(feature_requirements, dict):
        return True
    for key in ("fast_mode", "fastMode"):
        value = feature_requirements.get(key)
        if value is False:
            return False
        if isinstance(value, dict) and any(value.get(field) is False for field in ("enabled", "value", "required")):
            return False
    return True


def _public_warnings(payload: dict) -> list[str]:
    warnings: list[str] = []
    raw = payload.get("warnings")
    if isinstance(raw, dict):
        warnings.extend(warning for value in raw.values() if (warning := _public_text(value)))
    elif isinstance(raw, list):
        warnings.extend(warning for value in raw if (warning := _public_text(value)))
    reasoning_warning = _public_text(payload.get("reasoningOptionsWarning"))
    if reasoning_warning:
        warnings.append(reasoning_warning)
    return list(dict.fromkeys(warnings))


def _optional_text(value: object, *, label: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    normalized = value.strip()
    if not normalized or normalized.lower() == "default":
        return None
    if len(normalized) > max_chars or any(character in normalized for character in "\r\n\x00"):
        raise ValueError(f"{label} is invalid")
    return normalized


def _first(config: dict, *keys: str) -> object | None:
    for key in keys:
        if key in config and config.get(key) is not None:
            return config[key]
    return None


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:500] or None


def _public_text(value: object) -> str | None:
    return _string_or_none(value)


def _is_public_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _public_scalar(value: object) -> object:
    return value[:500] if isinstance(value, str) else value

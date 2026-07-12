from __future__ import annotations

import pytest

from imcodex.admin.native import (
    apply_global_setting,
    public_native_settings,
    public_write_result,
)


def test_public_native_settings_whitelists_config_and_catalog_fields() -> None:
    payload = {
        "config": {
            "model": "gpt-test",
            "model_reasoning_effort": "high",
            "personality": None,
            "service_tier": "priority",
            "features": {"fast_mode": False, "secret_feature": "do-not-return"},
            "default_permissions": ":danger-full-access",
            "approval_policy": "never",
            "mcp_servers": {"private": {"env": {"API_KEY": "secret"}}},
        },
        "layers": [{"config": {"OPENAI_API_KEY": "secret"}}],
        "models": [
            {
                "id": "gpt-test",
                "displayName": "GPT Test",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "More reasoning"},
                ],
                "defaultServiceTier": "default",
                "serviceTiers": [
                    {
                        "id": "priority",
                        "name": "Fast",
                        "description": "Faster responses",
                        "hiddenToken": "secret",
                    }
                ],
                "additionalSpeedTiers": ["fast", {"secret": "drop-me"}],
                "hiddenToken": "secret",
            }
        ],
        "reasoningEfforts": [{"reasoningEffort": "high", "description": "More reasoning"}],
        "profiles": [{"id": ":danger-full-access", "description": "Native full access"}],
        "nativeProfilesSupported": True,
        "requirements": {"allowedPermissionProfiles": {":danger-full-access": True}},
    }

    public = public_native_settings(payload, csrf_token="csrf-test")

    assert public["csrfToken"] == "csrf-test"
    assert public["config"] == {
        "model": "gpt-test",
        "reasoningEffort": "high",
        "personality": "default",
        "fast": False,
        "permissionMode": "full-access",
    }
    assert public["models"] == [
        {
            "id": "gpt-test",
            "displayName": "GPT Test",
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "medium", "description": "Balanced"},
                {"reasoningEffort": "high", "description": "More reasoning"},
            ],
            "defaultServiceTier": "default",
            "serviceTiers": [
                {
                    "id": "priority",
                    "name": "Fast",
                    "description": "Faster responses",
                }
            ],
            "additionalSpeedTiers": ["fast"],
        }
    ]
    assert public["fastAvailable"] is False
    full_access = next(mode for mode in public["permissionModes"] if mode["id"] == "full-access")
    assert "without approval prompts" in full_access["description"]
    assert "secret" not in str(public)
    assert "mcp_servers" not in str(public)


@pytest.mark.parametrize(
    ("service_tier", "features", "enabled"),
    [
        ("priority", {"fast_mode": False}, False),
        ("fast", None, True),
        ("default", {"fast_mode": True}, False),
        (None, {"fast_mode": True}, False),
    ],
)
def test_public_native_settings_uses_service_tier_as_fast_mode_truth(
    service_tier: str | None,
    features: dict | None,
    enabled: bool,
) -> None:
    config = {"service_tier": service_tier}
    if features is not None:
        config["features"] = features

    public = public_native_settings({"config": config}, csrf_token="csrf-test")

    assert public["config"]["fast"] is enabled


def test_public_native_settings_drops_structured_values_from_text_fields() -> None:
    payload = {
        "config": {
            "model": {"secret": "model-secret"},
            "model_reasoning_effort": {"secret": "effort-secret"},
            "personality": {"secret": "personality-secret"},
        },
        "models": [
            {
                "id": "gpt-test",
                "displayName": {"secret": "display-secret"},
                "supportedReasoningEfforts": [
                    {
                        "reasoningEffort": "high",
                        "description": {"secret": "description-secret"},
                    }
                ],
            }
        ],
        "warnings": {"catalog": {"secret": "warning-secret"}},
    }

    public = public_native_settings(payload, csrf_token="csrf-test")

    assert public["config"]["model"] is None
    assert public["config"]["reasoningEffort"] is None
    assert public["config"]["personality"] == "default"
    assert public["models"][0]["displayName"] == "gpt-test"
    assert public["models"][0]["supportedReasoningEfforts"] == [{"reasoningEffort": "high"}]
    assert public["warnings"] == []
    assert "secret" not in repr(public)


def test_public_native_settings_preserves_absent_vs_empty_reasoning_metadata() -> None:
    public = public_native_settings(
        {
            "models": [
                {"id": "gpt-unknown"},
                {"id": "gpt-none", "supportedReasoningEfforts": []},
            ]
        },
        csrf_token="csrf-test",
    )

    assert "supportedReasoningEfforts" not in public["models"][0]
    assert public["models"][1]["supportedReasoningEfforts"] == []


def test_public_native_settings_projects_managed_defaults_and_keeps_selected_hidden_model() -> None:
    public = public_native_settings(
        {
            "config": {
                "model": "gpt-user",
                "service_tier": "default",
                "approval_policy": "on-request",
            },
            "effectiveGlobalConfig": {
                "model": "gpt-managed",
                "model_reasoning_effort": "high",
                "service_tier": "priority",
                "default_permissions": ":read-only",
                "approval_policy": "on-request",
            },
            "selectedModel": "gpt-managed",
            "managedSettings": ["model", "reasoningEffort", "fast", "permissionMode", "drop"],
            "models": [
                {"id": "gpt-hidden-other", "hidden": True},
                {
                    "id": "gpt-managed",
                    "hidden": True,
                    "supportsPersonality": False,
                    "defaultServiceTier": "default",
                    "serviceTiers": [{"id": "priority", "name": "Fast"}],
                },
                {"id": "gpt-visible"},
            ],
        },
        csrf_token="csrf-test",
    )

    assert public["config"] == {
        "model": "gpt-managed",
        "reasoningEffort": "high",
        "personality": "default",
        "fast": True,
        "permissionMode": "read-only",
    }
    assert [model["id"] for model in public["models"]] == ["gpt-managed", "gpt-visible"]
    assert public["models"][0]["supportsPersonality"] is False
    assert public["readOnlySettings"] == ["model", "reasoningEffort", "fast", "permissionMode"]


def test_public_native_settings_uses_model_default_fast_only_without_explicit_tier() -> None:
    model = {"id": "gpt-fast", "isDefault": True, "defaultServiceTier": "priority"}

    inherited = public_native_settings({"config": {}, "models": [model]}, csrf_token="csrf-test")
    explicit_standard = public_native_settings(
        {"config": {"service_tier": "default"}, "models": [model]},
        csrf_token="csrf-test",
    )

    assert inherited["config"]["fast"] is True
    assert explicit_standard["config"]["fast"] is False

    unsupported = public_native_settings(
        {
            "config": {"model": "gpt-mini", "service_tier": "priority"},
            "models": [{"id": "gpt-mini", "serviceTiers": []}],
        },
        csrf_token="csrf-test",
    )
    assert unsupported["config"]["fast"] is False


def test_public_native_settings_combines_personality_feature_and_model_capabilities() -> None:
    feature_disabled = public_native_settings(
        {
            "personalityAvailable": False,
            "models": [{"id": "gpt-capable", "isDefault": True, "supportsPersonality": True}],
        },
        csrf_token="csrf-test",
    )
    model_disabled = public_native_settings(
        {
            "personalityAvailable": True,
            "models": [{"id": "gpt-mini", "isDefault": True, "supportsPersonality": False}],
        },
        csrf_token="csrf-test",
    )

    assert feature_disabled["personalityAvailable"] is False
    assert model_disabled["personalityAvailable"] is False


def test_permission_profile_without_matching_approval_is_custom() -> None:
    public = public_native_settings(
        {"config": {"default_permissions": ":danger-full-access", "approval_policy": "on-request"}},
        csrf_token="csrf-test",
    )

    assert public["config"]["permissionMode"] == "custom"

    implicit_default = public_native_settings(
        {"config": {"default_permissions": ":workspace"}},
        csrf_token="csrf-test",
    )
    implicit_full_access = public_native_settings(
        {"config": {"default_permissions": ":danger-full-access"}},
        csrf_token="csrf-test",
    )
    assert implicit_default["config"]["permissionMode"] == "default"
    assert implicit_full_access["config"]["permissionMode"] == "custom"


@pytest.mark.asyncio
async def test_apply_global_setting_uses_only_whitelisted_backend_methods() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def set_global_model(self, value):
            self.calls.append(("model", value))
            return {"status": "ok"}

        async def set_global_preferences(self, value):
            self.calls.append(("preferences", value))
            return {"status": "ok"}

        async def set_global_reasoning_effort(self, value):
            self.calls.append(("reasoning", value))
            return {"status": "ok"}

        async def set_global_personality(self, value):
            self.calls.append(("personality", value))
            return {"status": "ok"}

        async def set_global_fast_mode(self, value):
            self.calls.append(("fast", value))
            return {"status": "ok"}

        async def set_global_permission_mode(self, value):
            self.calls.append(("permission", value))
            return {"status": "ok"}

    backend = Backend()

    await apply_global_setting(backend, setting="model", value="default")
    await apply_global_setting(
        backend,
        setting="preferences",
        value={
            "model": "gpt-next",
            "reasoningEffort": "default",
            "personality": "friendly",
            "fast": False,
        },
    )
    await apply_global_setting(backend, setting="reasoningEffort", value="high")
    await apply_global_setting(backend, setting="personality", value="default")
    await apply_global_setting(backend, setting="fast", value=True)
    await apply_global_setting(backend, setting="permissionMode", value="full-access")

    assert backend.calls == [
        ("model", None),
        (
            "preferences",
            {
                "model": "gpt-next",
                "reasoningEffort": None,
                "personality": "friendly",
                "fast": False,
            },
        ),
        ("reasoning", "high"),
        ("personality", None),
        ("fast", True),
        ("permission", "full-access"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("native.call", "thread/delete"),
        ("personality", "chaotic"),
        ("fast", "yes"),
        ("permissionMode", "unrestricted"),
        ("model", "bad\nmodel"),
        ("preferences", {"permissionMode": "default"}),
    ],
)
async def test_apply_global_setting_rejects_unapproved_shapes(setting: str, value: object) -> None:
    with pytest.raises(ValueError):
        await apply_global_setting(object(), setting=setting, value=value)


def test_public_write_result_does_not_return_native_metadata() -> None:
    result = public_write_result(
        {
            "status": "updated",
            "mode": "full-access",
            "filePath": "/Users/private/.codex/config.toml",
            "overriddenMetadata": {"secret": "value"},
        }
    )

    assert result == {"status": "updated", "mode": "full-access"}

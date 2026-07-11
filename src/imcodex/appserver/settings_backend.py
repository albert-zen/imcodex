from __future__ import annotations

from .client import AppServerError


PERMISSION_MODE_PROFILE_IDS = {
    "default": ":workspace",
    "read-only": ":read-only",
    "full-access": ":danger-full-access",
}
_PERMISSION_MODE_APPROVAL_POLICIES = {
    "default": "on-request",
    "read-only": "on-request",
    "full-access": "never",
}
_LEGACY_PERMISSION_PRESETS = {
    "default": [
        {"keyPath": "approval_policy", "value": "on-request", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "workspace-write", "mergeStrategy": "replace"},
    ],
    "read-only": [
        {"keyPath": "approval_policy", "value": "on-request", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "read-only", "mergeStrategy": "replace"},
    ],
    "full-access": [
        {"keyPath": "approval_policy", "value": "never", "mergeStrategy": "replace"},
        {"keyPath": "sandbox_mode", "value": "danger-full-access", "mergeStrategy": "replace"},
    ],
}
_FALLBACK_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_NATIVE_PERMISSION_CONFIG_KEYS = (
    "default_permissions",
    "permissionProfile",
    "approval_policy",
    "approvalPolicy",
    "sandbox_mode",
    "sandboxMode",
    "sandbox",
)


class CodexSettingsBackendMixin:
    async def list_models(self) -> dict:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        models: list[dict] = []
        combined: dict = {}
        while True:
            params: dict[str, object] = {}
            if cursor is not None:
                params["cursor"] = cursor
            page = await self.client.list_models(params)
            if not combined:
                combined = dict(page)
            models.extend(item for item in page.get("data", []) if isinstance(item, dict))
            next_cursor = page.get("nextCursor")
            if not next_cursor:
                combined["data"] = models
                combined["nextCursor"] = None
                return combined
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise AppServerError("model/list returned a repeated pagination cursor")
            seen_cursors.add(cursor)

    async def read_account_rate_limits(self) -> dict:
        return await self.client.read_account_rate_limits()

    async def read_account_usage(self) -> dict:
        return await self.client.read_account_usage()

    async def read_account_credits(self) -> dict:
        result: dict = {}
        warnings: dict[str, str] = {}
        try:
            result["rateLimitsResult"] = await self.read_account_rate_limits()
        except AppServerError as exc:
            warnings["rateLimits"] = str(exc)
        try:
            result["usageResult"] = await self.read_account_usage()
        except AppServerError as exc:
            warnings["usage"] = str(exc)
        if warnings:
            result["warnings"] = warnings
        if "rateLimitsResult" not in result and "usageResult" not in result:
            raise AppServerError("account rate limits and usage are unavailable")
        return result

    async def read_permission_options(self, channel_id: str, conversation_id: str) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self._read_permission_options_for_cwd(cwd)

    async def _read_permission_options_for_cwd(
        self,
        cwd: str | None,
        *,
        config_result: dict | None = None,
        include_layers: bool = False,
    ) -> dict:
        result = (
            config_result
            if config_result is not None
            else await self.client.read_config(include_layers=include_layers, cwd=cwd)
        )
        warnings: dict[str, str] = {}
        try:
            result["profiles"] = await self._list_permission_profiles_for_cwd(cwd)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            warnings["profiles"] = str(exc)
            result["profiles"] = []
            result["nativeProfilesSupported"] = False
        else:
            result["nativeProfilesSupported"] = True
        try:
            result.update(await self.client.read_config_requirements())
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            warnings["requirements"] = str(exc)
        if warnings:
            result["warnings"] = warnings
        return result

    async def read_reasoning_options(self, channel_id: str, conversation_id: str) -> dict:
        result = await self.read_config(channel_id, conversation_id)
        config = self._effective_config(result)
        try:
            catalog = await self.list_models()
        except AppServerError as exc:
            result["reasoningEfforts"] = self._fallback_reasoning_efforts()
            result["reasoningOptionsSource"] = "fallback"
            result["reasoningOptionsWarning"] = str(exc)
            result["selectedModel"] = self._configured_model_id(config)
            return result

        models = [item for item in catalog.get("data", []) if isinstance(item, dict)]
        selected = self._select_reasoning_model(config, models)
        if selected is None:
            result["reasoningEfforts"] = self._fallback_reasoning_efforts()
            result["reasoningOptionsSource"] = "fallback"
            result["reasoningOptionsWarning"] = "the active model was not found in the native model catalog"
            result["selectedModel"] = self._configured_model_id(config)
            return result

        efforts = self._native_reasoning_efforts(selected)
        if efforts is None:
            result["reasoningEfforts"] = self._fallback_reasoning_efforts()
            result["reasoningOptionsSource"] = "fallback"
            result["reasoningOptionsWarning"] = "the native model catalog did not include reasoning effort metadata"
        else:
            result["reasoningEfforts"] = efforts
            result["reasoningOptionsSource"] = "native"
        result["selectedModel"] = str(selected.get("id") or selected.get("model") or "") or None
        result["selectedModelDisplayName"] = str(selected.get("displayName") or "") or None
        result["defaultReasoningEffort"] = selected.get("defaultReasoningEffort")
        return result

    async def set_reasoning_effort(
        self,
        channel_id: str,
        conversation_id: str,
        effort: str | None,
    ) -> dict:
        if effort is not None:
            options = await self.read_reasoning_options(channel_id, conversation_id)
            supported = {
                str(item.get("reasoningEffort") or "").lower()
                for item in options.get("reasoningEfforts", [])
                if isinstance(item, dict) and item.get("reasoningEffort")
            }
            normalized = effort.lower()
            if normalized not in supported:
                model = str(
                    options.get("selectedModelDisplayName")
                    or options.get("selectedModel")
                    or "the active model"
                )
                available = ", ".join(sorted(supported)) or "none"
                raise AppServerError(
                    f"reasoning effort {normalized} is not supported by {model}; available efforts: {available}"
                )
            effort = normalized
        return await self.batch_write_config(
            edits=[
                {
                    "keyPath": "model_reasoning_effort",
                    "value": effort,
                    "mergeStrategy": "replace",
                }
            ],
            reload_user_config=True,
        )

    async def set_permission_mode(self, channel_id: str, conversation_id: str, mode: str) -> dict:
        profile_id = PERMISSION_MODE_PROFILE_IDS.get(mode)
        if profile_id is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        cwd = self.store.current_cwd(channel_id, conversation_id)
        try:
            options = await self._read_permission_options_for_cwd(cwd, include_layers=True)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            return await self._set_legacy_permission_mode(mode, warning=str(exc))
        if options.get("nativeProfilesSupported") is False:
            warning = str((options.get("warnings") or {}).get("profiles") or "")
            if not self._legacy_permission_mode_is_allowed(mode, options.get("requirements")):
                raise AppServerError(f"permission mode {mode} is not allowed by Codex requirements")
            return await self._set_legacy_permission_mode(mode, warning=warning, config_result=options)
        if not self._permission_profile_is_available(profile_id, options):
            raise AppServerError(f"permission profile {profile_id} is not available in Codex")
        if not self._permission_profile_is_allowed(profile_id, options):
            raise AppServerError(f"permission profile {profile_id} is not allowed by Codex requirements")
        if not self._approval_policy_is_allowed(mode, options.get("requirements")):
            raise AppServerError(f"approval policy for {mode} is not allowed by Codex requirements")
        write_result = await self._set_native_permission_mode(mode, profile_id, config_result=options)
        write_result["mode"] = mode
        write_result["profile"] = profile_id
        write_result["fallback"] = False
        return write_result

    async def ensure_default_permission_mode(self, connection_epoch: int | None = None) -> dict:
        del connection_epoch
        result = await self.client.read_config(include_layers=True, cwd=None)
        config = self._effective_config(result)
        if any(config.get(key) is not None for key in _NATIVE_PERMISSION_CONFIG_KEYS):
            return {"changed": False, "reason": "native permission configuration already exists"}

        try:
            options = await self._read_permission_options_for_cwd(None, config_result=result)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            write_result = await self._set_legacy_permission_mode(
                "full-access",
                warning=str(exc),
                config_result=result,
            )
        else:
            managed_default = self._requirements_default_permission(options.get("requirements"))
            if managed_default is not None:
                return {
                    "changed": False,
                    "reason": f"Codex requirements define the permission default {managed_default}",
                }
            if options.get("nativeProfilesSupported") is False:
                warning = str((options.get("warnings") or {}).get("profiles") or "")
                if not self._legacy_permission_mode_is_allowed("full-access", options.get("requirements")):
                    return {
                        "changed": False,
                        "reason": "Codex requirements do not allow the documented full-access default",
                    }
                write_result = await self._set_legacy_permission_mode(
                    "full-access",
                    warning=warning,
                    config_result=options,
                )
            else:
                profile_id = PERMISSION_MODE_PROFILE_IDS["full-access"]
                if not self._permission_profile_is_available(profile_id, options):
                    return {
                        "changed": False,
                        "reason": f"permission profile {profile_id} is not available in Codex",
                    }
                if not self._permission_profile_is_allowed(profile_id, options):
                    return {
                        "changed": False,
                        "reason": f"permission profile {profile_id} is not allowed by Codex requirements",
                    }
                if not self._approval_policy_is_allowed("full-access", options.get("requirements")):
                    return {
                        "changed": False,
                        "reason": "approval policy never is not allowed by Codex requirements",
                    }
                write_result = await self._set_native_permission_mode(
                    "full-access",
                    profile_id,
                    config_result=options,
                )
                write_result["mode"] = "full-access"
                write_result["profile"] = profile_id
                write_result["fallback"] = False
        write_result["changed"] = True
        return write_result

    async def read_config(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        include_layers: bool = False,
    ) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self.client.read_config(include_layers=include_layers, cwd=cwd)

    async def write_config_value(
        self,
        *,
        key_path: str,
        value: object,
        merge_strategy: str = "replace",
    ) -> dict:
        return await self.client.write_config_value(
            key_path=key_path,
            value=value,
            merge_strategy=merge_strategy,
        )

    async def batch_write_config(
        self,
        *,
        edits: list[dict],
        reload_user_config: bool = False,
        expected_version: str | None = None,
        file_path: str | None = None,
    ) -> dict:
        return await self.client.batch_write_config(
            edits=edits,
            reload_user_config=reload_user_config,
            expected_version=expected_version,
            file_path=file_path,
        )

    async def set_default_model(self, model: str | None) -> dict:
        return await self.write_config_value(key_path="model", value=model, merge_strategy="replace")

    async def set_default_personality(self, personality: str | None) -> dict:
        return await self.batch_write_config(
            edits=[
                {
                    "keyPath": "personality",
                    "value": personality,
                    "mergeStrategy": "replace",
                }
            ],
            reload_user_config=True,
        )

    async def call_native(self, method: str, params: dict | None = None) -> dict:
        return await self.client.call(method, params)

    async def _list_permission_profiles(self, channel_id: str, conversation_id: str) -> list[dict]:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self._list_permission_profiles_for_cwd(cwd)

    async def _list_permission_profiles_for_cwd(self, cwd: str | None) -> list[dict]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        profiles: list[dict] = []
        while True:
            params: dict[str, object] = {}
            if cwd is not None:
                params["cwd"] = cwd
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.client.list_permission_profiles(params)
            profiles.extend(item for item in result.get("data", []) if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return profiles
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise AppServerError("permissionProfile/list returned a repeated pagination cursor")
            seen_cursors.add(cursor)

    async def _set_legacy_permission_mode(
        self,
        mode: str,
        *,
        warning: str = "",
        config_result: dict | None = None,
    ) -> dict:
        edits = _LEGACY_PERMISSION_PRESETS.get(mode)
        if edits is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        expected_version, file_path = self._user_config_write_target(config_result)
        result = await self.batch_write_config(
            edits=edits,
            reload_user_config=True,
            expected_version=expected_version,
            file_path=file_path,
        )
        result["mode"] = mode
        result["fallback"] = True
        if warning:
            result["warning"] = warning
        return result

    def _permission_profile_is_available(self, profile_id: str, options: dict) -> bool:
        profiles = options.get("profiles")
        if not isinstance(profiles, list):
            return False
        return any(isinstance(profile, dict) and profile.get("id") == profile_id for profile in profiles)

    def _permission_profile_is_allowed(self, profile_id: str, options: dict) -> bool:
        profiles = options.get("profiles")
        if isinstance(profiles, list):
            profile = next(
                (item for item in profiles if isinstance(item, dict) and item.get("id") == profile_id),
                None,
            )
            if isinstance(profile, dict) and profile.get("allowed") is False:
                return False
        requirements = options.get("requirements")
        if not isinstance(requirements, dict):
            return True
        allowed = requirements.get("allowedPermissionProfiles")
        if not isinstance(allowed, dict):
            return True
        return bool(allowed.get(profile_id))

    def _approval_policy_is_allowed(self, mode: str, requirements: object) -> bool:
        if not isinstance(requirements, dict):
            return True
        allowed = requirements.get("allowedApprovalPolicies")
        if not isinstance(allowed, list):
            return True
        return _PERMISSION_MODE_APPROVAL_POLICIES[mode] in allowed

    def _legacy_permission_mode_is_allowed(self, mode: str, requirements: object) -> bool:
        if not self._approval_policy_is_allowed(mode, requirements):
            return False
        if not isinstance(requirements, dict):
            return True
        allowed_sandboxes = requirements.get("allowedSandboxModes")
        if not isinstance(allowed_sandboxes, list):
            return True
        edits = _LEGACY_PERMISSION_PRESETS[mode]
        sandbox = next(edit["value"] for edit in edits if edit["keyPath"] == "sandbox_mode")
        return sandbox in allowed_sandboxes

    def _requirements_default_permission(self, requirements: object) -> object | None:
        if not isinstance(requirements, dict):
            return None
        for key in ("defaultPermissions", "defaultPermissionProfile", "default_permissions"):
            if requirements.get(key) is not None:
                return requirements[key]
        return None

    async def _set_native_permission_mode(
        self,
        mode: str,
        profile_id: str,
        *,
        config_result: dict | None = None,
    ) -> dict:
        approval_policy = _PERMISSION_MODE_APPROVAL_POLICIES[mode]
        expected_version, file_path = self._user_config_write_target(config_result)
        return await self.batch_write_config(
            edits=[
                {
                    "keyPath": "default_permissions",
                    "value": profile_id,
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "approval_policy",
                    "value": approval_policy,
                    "mergeStrategy": "replace",
                },
                {
                    "keyPath": "sandbox_mode",
                    "value": None,
                    "mergeStrategy": "replace",
                },
            ],
            reload_user_config=True,
            expected_version=expected_version,
            file_path=file_path,
        )

    def _user_config_write_target(self, config_result: dict | None) -> tuple[str | None, str | None]:
        if not isinstance(config_result, dict):
            return None, None
        layers = config_result.get("layers")
        if not isinstance(layers, list):
            return None, None
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            source = layer.get("name")
            if not isinstance(source, dict) or source.get("type") != "user" or source.get("profile"):
                continue
            version = str(layer.get("version") or "").strip() or None
            file_path = str(source.get("file") or "").strip() or None
            return version, file_path
        return None, None

    def _effective_config(self, payload: dict) -> dict:
        config = payload.get("config")
        return config if isinstance(config, dict) else payload

    def _configured_model_id(self, config: dict) -> str | None:
        for key in ("model", "modelId", "modelID"):
            value = config.get(key)
            if value:
                return str(value)
        return None

    def _select_reasoning_model(self, config: dict, models: list[dict]) -> dict | None:
        configured = self._configured_model_id(config)
        if configured is not None:
            return next(
                (
                    model
                    for model in models
                    if configured in {str(model.get("id") or ""), str(model.get("model") or "")}
                ),
                None,
            )
        default = next((model for model in models if model.get("isDefault")), None)
        return default or (models[0] if models else None)

    def _native_reasoning_efforts(self, model: dict) -> list[dict] | None:
        raw_efforts = model.get("supportedReasoningEfforts")
        if not isinstance(raw_efforts, list):
            return None
        efforts: list[dict] = []
        seen: set[str] = set()
        for item in raw_efforts:
            if isinstance(item, str):
                effort = item.strip().lower()
                description = ""
            elif isinstance(item, dict):
                effort = str(item.get("reasoningEffort") or item.get("effort") or "").strip().lower()
                description = str(item.get("description") or "").strip()
            else:
                continue
            if not effort or effort in seen:
                continue
            seen.add(effort)
            normalized = {"reasoningEffort": effort}
            if description:
                normalized["description"] = description
            efforts.append(normalized)
        return efforts

    def _fallback_reasoning_efforts(self) -> list[dict]:
        return [{"reasoningEffort": effort} for effort in _FALLBACK_REASONING_EFFORTS]

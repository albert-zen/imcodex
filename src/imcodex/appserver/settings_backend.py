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
        {
            "keyPath": "approval_policy",
            "value": "on-request",
            "mergeStrategy": "replace",
        },
        {
            "keyPath": "sandbox_mode",
            "value": "workspace-write",
            "mergeStrategy": "replace",
        },
    ],
    "read-only": [
        {
            "keyPath": "approval_policy",
            "value": "on-request",
            "mergeStrategy": "replace",
        },
        {"keyPath": "sandbox_mode", "value": "read-only", "mergeStrategy": "replace"},
    ],
    "full-access": [
        {"keyPath": "approval_policy", "value": "never", "mergeStrategy": "replace"},
        {
            "keyPath": "sandbox_mode",
            "value": "danger-full-access",
            "mergeStrategy": "replace",
        },
    ],
}
_FALLBACK_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
_PERSONALITIES = {"none", "friendly", "pragmatic"}
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
    async def list_models(self, *, include_hidden: bool = False) -> dict:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        models: list[dict] = []
        combined: dict = {}
        while True:
            params: dict[str, object] = {}
            if include_hidden:
                params["includeHidden"] = True
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

    async def read_global_settings(self) -> dict:
        result = await self.read_global_config()
        warnings: dict[str, str] = {}
        try:
            result.update(await self.client.read_config_requirements())
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            warnings["requirements"] = str(exc)

        config = self._effective_config(result)
        requirements = result.get("requirements")
        effective_config, managed_settings = self._global_effective_config(config, requirements)
        result["effectiveGlobalConfig"] = effective_config
        result["managedSettings"] = managed_settings
        result["fastAvailable"] = self._fast_feature_available(config, requirements)
        result["personalityAvailable"] = self._personality_feature_available(config, requirements)
        result = await self._read_reasoning_options_for_cwd(
            None,
            config_result=result,
            include_models=True,
            include_hidden=True,
            effective_config=effective_config,
        )
        result = await self._read_permission_options_for_cwd(
            None,
            config_result=result,
            read_requirements=False,
        )
        if warnings:
            result.setdefault("warnings", {}).update(warnings)
        return result

    async def read_permission_options(self, channel_id: str, conversation_id: str) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self._read_permission_options_for_cwd(cwd)

    async def read_global_permission_options(self) -> dict:
        result = await self.read_global_config()
        return await self._read_permission_options_for_cwd(None, config_result=result)

    async def _read_permission_options_for_cwd(
        self,
        cwd: str | None,
        *,
        config_result: dict | None = None,
        include_layers: bool = False,
        read_requirements: bool = True,
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
        if read_requirements:
            try:
                result.update(await self.client.read_config_requirements())
            except AppServerError as exc:
                if not self._is_native_permission_profile_unsupported(exc):
                    raise
                warnings["requirements"] = str(exc)
        effective_config, _managed = self._global_effective_config(
            self._effective_config(result),
            result.get("requirements"),
        )
        result["effectiveConfig"] = effective_config
        if warnings:
            result["warnings"] = warnings
        return result

    async def read_reasoning_options(self, channel_id: str, conversation_id: str) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        result = await self.client.read_config(include_layers=False, cwd=cwd)
        result.update(await self._read_config_requirements_if_supported())
        effective_config, _managed = self._global_effective_config(
            self._effective_config(result),
            result.get("requirements"),
        )
        result["effectiveConfig"] = effective_config
        return await self._read_reasoning_options_for_cwd(
            cwd,
            config_result=result,
            effective_config=effective_config,
        )

    async def read_global_reasoning_options(self) -> dict:
        result = await self.read_global_config()
        return await self._read_reasoning_options_for_cwd(
            None,
            config_result=result,
            include_models=True,
        )

    async def read_fast_options(self, channel_id: str, conversation_id: str) -> dict:
        return await self.read_effective_settings(channel_id, conversation_id)

    async def read_personality_options(self, channel_id: str, conversation_id: str) -> dict:
        result = await self.read_effective_settings(channel_id, conversation_id)
        result["personalityAvailable"] = (
            result.get("personalityFeatureAvailable") is not False
            and result.get("personalitySupported") is not False
        )
        return result

    async def read_effective_settings(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        include_model_metadata: bool = True,
    ) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        result = await self.client.read_config(include_layers=False, cwd=cwd)
        result.update(await self._read_config_requirements_if_supported())
        config = self._effective_config(result)
        requirements = result.get("requirements")
        effective_config, _managed = self._global_effective_config(config, requirements)
        result["effectiveConfig"] = effective_config
        result["fastAvailable"] = self._fast_feature_available(config, requirements)
        result["personalityFeatureAvailable"] = self._personality_feature_available(config, requirements)
        if not include_model_metadata:
            return result
        return await self.enrich_effective_settings(result)

    async def enrich_effective_settings(self, result: dict) -> dict:
        effective_config = result.get("effectiveConfig")
        if not isinstance(effective_config, dict):
            effective_config = self._effective_config(result)
        try:
            catalog = await self.list_models(include_hidden=True)
        except AppServerError as exc:
            result["fastOptionsWarning"] = str(exc)
            return result
        selected = self._select_reasoning_model(
            effective_config,
            [item for item in catalog.get("data", []) if isinstance(item, dict)],
        )
        if selected is not None:
            result["selectedModelDefaultServiceTier"] = selected.get("defaultServiceTier")
            result["fastSupported"] = self._model_supports_fast_tier(selected)
            result["personalitySupported"] = selected.get("supportsPersonality") is not False
        return result

    async def _read_reasoning_options_for_cwd(
        self,
        cwd: str | None,
        *,
        config_result: dict | None = None,
        include_models: bool = False,
        include_hidden: bool = False,
        effective_config: dict | None = None,
    ) -> dict:
        result = (
            config_result if config_result is not None else await self.client.read_config(include_layers=False, cwd=cwd)
        )
        config = effective_config if effective_config is not None else self._effective_config(result)
        try:
            catalog = await self.list_models(include_hidden=include_hidden)
        except AppServerError as exc:
            if include_models:
                result["models"] = []
            result["reasoningEfforts"] = self._fallback_reasoning_efforts()
            result["reasoningOptionsSource"] = "fallback"
            result["reasoningOptionsWarning"] = str(exc)
            result["selectedModel"] = self._configured_model_id(config)
            return result

        models = [item for item in catalog.get("data", []) if isinstance(item, dict)]
        if include_models:
            result["models"] = models
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
        options = await self.read_reasoning_options(channel_id, conversation_id)
        if self._managed_new_thread_value(
            options.get("requirements"),
            "modelReasoningEffort",
            "model_reasoning_effort",
        ) is not None:
            raise AppServerError("reasoning effort is managed by Codex requirements")
        return await self._set_reasoning_effort_value(effort, options=options)

    async def set_global_reasoning_effort(self, effort: str | None) -> dict:
        config_result = await self.read_global_config()
        requirements_result = await self._read_config_requirements_if_supported()
        config_result.update(requirements_result)
        requirements = config_result.get("requirements")
        if self._managed_new_thread_value(requirements, "modelReasoningEffort", "model_reasoning_effort") is not None:
            raise AppServerError("reasoning effort is managed by Codex requirements")
        if isinstance(effort, str) and effort.lower() == "default":
            effort = None
        if effort is not None:
            effective_config, _managed = self._global_effective_config(
                self._effective_config(config_result),
                requirements,
            )
            config_result = await self._read_reasoning_options_for_cwd(
                None,
                config_result=config_result,
                include_models=True,
                include_hidden=True,
                effective_config=effective_config,
            )
        return await self._set_reasoning_effort_value(effort, options=config_result)

    async def _set_reasoning_effort_value(
        self,
        effort: str | None,
        *,
        options: dict | None,
    ) -> dict:
        if effort is not None:
            if options is None:
                raise AppServerError("reasoning options are required to validate a non-default effort")
            effort = self._validated_reasoning_effort(effort, options)
        return await self._write_user_config_edits(
            edits=[
                {
                    "keyPath": "model_reasoning_effort",
                    "value": effort,
                    "mergeStrategy": "replace",
                }
            ],
            reload_user_config=True,
            config_result=options,
        )

    async def set_permission_mode(self, channel_id: str, conversation_id: str, mode: str) -> dict:
        self._permission_profile_id(mode)
        cwd = self.store.current_cwd(channel_id, conversation_id)
        return await self._set_permission_mode_for_cwd(cwd, mode)

    async def set_global_permission_mode(self, mode: str) -> dict:
        self._permission_profile_id(mode)
        config_result = await self.read_global_config()
        return await self._set_permission_mode_for_cwd(None, mode, config_result=config_result)

    async def _set_permission_mode_for_cwd(
        self,
        cwd: str | None,
        mode: str,
        *,
        config_result: dict | None = None,
    ) -> dict:
        profile_id = self._permission_profile_id(mode)
        try:
            options = await self._read_permission_options_for_cwd(
                cwd,
                config_result=config_result,
                include_layers=True,
            )
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            return await self._set_legacy_permission_mode(
                mode,
                warning=str(exc),
                config_result=config_result,
            )
        if self._requirements_default_permission(options.get("requirements")) is not None:
            raise AppServerError("permission mode is managed by Codex requirements")
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
            return {
                "changed": False,
                "reason": "native permission configuration already exists",
            }

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

    async def read_global_config(self, *, include_layers: bool = True) -> dict:
        return await self.client.read_config(include_layers=include_layers, cwd=None)

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
        return await self._set_model_value(model)

    async def set_model(
        self,
        channel_id: str,
        conversation_id: str,
        model: str | None,
    ) -> dict:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        config_result = await self.client.read_config(include_layers=False, cwd=cwd)
        config_result.update(await self._read_config_requirements_if_supported())
        requirements = config_result.get("requirements")
        if self._managed_new_thread_value(requirements, "model") is not None:
            raise AppServerError("model is managed by Codex requirements")
        if isinstance(model, str) and model.lower() == "default":
            model = None
        await self._validate_model_compatibility(model, config_result)
        return await self._set_model_value(model)

    async def set_global_model(self, model: str | None) -> dict:
        config_result = await self.read_global_config()
        requirements_result = await self._read_config_requirements_if_supported()
        config_result.update(requirements_result)
        requirements = config_result.get("requirements")
        if self._managed_new_thread_value(requirements, "model") is not None:
            raise AppServerError("model is managed by Codex requirements")
        if isinstance(model, str) and model.lower() == "default":
            model = None
        await self._validate_model_compatibility(model, config_result)
        return await self._set_model_value(model, config_result=config_result)

    async def _set_model_value(
        self,
        model: str | None,
        *,
        config_result: dict | None = None,
    ) -> dict:
        if config_result is None:
            return await self.write_config_value(key_path="model", value=model, merge_strategy="replace")
        return await self._write_user_config_edits(
            edits=[{"keyPath": "model", "value": model, "mergeStrategy": "replace"}],
            reload_user_config=False,
            config_result=config_result,
        )

    async def set_default_personality(self, personality: str | None) -> dict:
        return await self._set_personality_value(personality)

    async def set_personality(
        self,
        channel_id: str,
        conversation_id: str,
        personality: str | None,
    ) -> dict:
        personality = self._normalized_personality(personality)
        cwd = self.store.current_cwd(channel_id, conversation_id)
        config_result = await self.client.read_config(include_layers=False, cwd=cwd)
        config_result.update(await self._read_config_requirements_if_supported())
        await self._validate_personality_compatibility(personality, config_result)
        return await self._set_personality_value(personality, config_result=config_result)

    async def set_global_personality(self, personality: str | None) -> dict:
        personality = self._normalized_personality(personality)
        config_result = await self.read_global_config()
        if personality is not None:
            requirements_result = await self._read_config_requirements_if_supported()
            config_result.update(requirements_result)
            await self._validate_personality_compatibility(personality, config_result)
        return await self._set_personality_value(personality, config_result=config_result)

    async def _set_personality_value(
        self,
        personality: str | None,
        *,
        config_result: dict | None = None,
    ) -> dict:
        return await self._write_user_config_edits(
            edits=[
                {
                    "keyPath": "personality",
                    "value": personality,
                    "mergeStrategy": "replace",
                }
            ],
            reload_user_config=True,
            config_result=config_result,
        )

    async def set_global_fast_mode(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise AppServerError("fast mode must be a boolean")
        config_result = await self.read_global_config()
        requirements_result = await self._read_config_requirements_if_supported()
        config_result.update(requirements_result)
        requirements = config_result.get("requirements")
        if self._managed_new_thread_value(requirements, "serviceTier", "service_tier") is not None:
            raise AppServerError("Fast mode is managed by Codex requirements")
        if enabled:
            config = self._effective_config(config_result)
            if not self._fast_feature_available(config, requirements):
                raise AppServerError("Fast mode is disabled by native Codex feature requirements")
            catalog = await self.list_models(include_hidden=True)
            models = [item for item in catalog.get("data", []) if isinstance(item, dict)]
            effective_config, _managed = self._global_effective_config(config, requirements)
            selected = self._select_reasoning_model(effective_config, models)
            if selected is None or not self._model_supports_fast_tier(selected):
                model = str(
                    (selected or {}).get("displayName")
                    or (selected or {}).get("id")
                    or self._configured_model_id(config)
                    or "the active model"
                )
                raise AppServerError(f"Fast mode is not available for {model} in the native model catalog")
        return await self._write_user_config_edits(
            edits=[
                {
                    "keyPath": "service_tier",
                    "value": "priority" if enabled else "default",
                    "mergeStrategy": "replace",
                },
            ],
            reload_user_config=False,
            config_result=config_result,
        )

    async def set_global_preferences(self, updates: dict[str, object]) -> dict:
        allowed = {"model", "reasoningEffort", "personality", "fast"}
        if not updates or not set(updates).issubset(allowed):
            raise AppServerError("native preference update contains unsupported settings")

        config_result = await self.read_global_config()
        config_result.update(await self._read_config_requirements_if_supported())
        config = self._effective_config(config_result)
        requirements = config_result.get("requirements")
        candidate = dict(config)
        edits: list[dict] = []

        if "model" in updates:
            if self._managed_new_thread_value(requirements, "model") is not None:
                raise AppServerError("model is managed by Codex requirements")
            model = updates["model"]
            for key in ("model", "modelId", "modelID"):
                candidate.pop(key, None)
            if model is not None:
                candidate["model"] = model
            edits.append({"keyPath": "model", "value": model, "mergeStrategy": "replace"})

        if "reasoningEffort" in updates:
            if self._managed_new_thread_value(
                requirements,
                "modelReasoningEffort",
                "model_reasoning_effort",
            ) is not None:
                raise AppServerError("reasoning effort is managed by Codex requirements")
            effort = updates["reasoningEffort"]
            candidate["model_reasoning_effort"] = effort
            edits.append(
                {
                    "keyPath": "model_reasoning_effort",
                    "value": effort,
                    "mergeStrategy": "replace",
                }
            )

        if "personality" in updates:
            personality = self._normalized_personality(updates["personality"])
            if personality is not None and not self._personality_feature_available(config, requirements):
                raise AppServerError("personality is disabled by native Codex feature requirements")
            candidate["personality"] = personality
            edits.append(
                {
                    "keyPath": "personality",
                    "value": personality,
                    "mergeStrategy": "replace",
                }
            )

        if "fast" in updates:
            enabled = updates["fast"]
            if not isinstance(enabled, bool):
                raise AppServerError("fast mode must be a boolean")
            if self._managed_new_thread_value(requirements, "serviceTier", "service_tier") is not None:
                raise AppServerError("Fast mode is managed by Codex requirements")
            if enabled and not self._fast_feature_available(config, requirements):
                raise AppServerError("Fast mode is disabled by native Codex feature requirements")
            candidate["service_tier"] = "priority" if enabled else "default"
            edits.append(
                {
                    "keyPath": "service_tier",
                    "value": "priority" if enabled else "default",
                    "mergeStrategy": "replace",
                }
            )

        reload_user_config = bool({"reasoningEffort", "personality"} & set(updates))
        needs_catalog = (
            "model" in updates
            or ("reasoningEffort" in updates and updates["reasoningEffort"] is not None)
            or ("personality" in updates and updates["personality"] is not None)
            or updates.get("fast") is True
        )
        if not needs_catalog:
            return await self._write_user_config_edits(
                edits=edits,
                reload_user_config=reload_user_config,
                config_result=config_result,
            )

        effective_candidate, _managed = self._global_effective_config(candidate, requirements)
        catalog = await self.list_models(include_hidden=True)
        models = [item for item in catalog.get("data", []) if isinstance(item, dict)]
        selected = self._select_reasoning_model(effective_candidate, models)
        if selected is None:
            model = self._configured_model_id(effective_candidate) or "the native default"
            raise AppServerError(f"model {model} was not found in the native model catalog")

        effort = effective_candidate.get("model_reasoning_effort") or effective_candidate.get("reasoningEffort")
        native_efforts = self._native_reasoning_efforts(selected)
        if effort is not None and native_efforts is not None:
            supported = {item["reasoningEffort"] for item in native_efforts}
            if str(effort).lower() not in supported:
                raise AppServerError(
                    f"reasoning effort {effort} is not supported by the selected native model"
                )

        personality = str(effective_candidate.get("personality") or "").strip().lower()
        if (
            self._personality_feature_available(config, requirements)
            and personality
            and personality != "default"
            and selected.get("supportsPersonality") is False
        ):
            raise AppServerError("personality is not supported by the selected native model")

        tier = str(effective_candidate.get("service_tier") or "").strip().lower()
        if (
            self._fast_feature_available(config, requirements)
            and tier in {"fast", "priority"}
            and not self._model_supports_fast_tier(selected)
        ):
            raise AppServerError("Fast mode is not available for the selected native model")

        return await self._write_user_config_edits(
            edits=edits,
            reload_user_config=reload_user_config,
            config_result=config_result,
        )

    async def set_fast_mode(
        self,
        channel_id: str,
        conversation_id: str,
        enabled: bool,
    ) -> dict:
        if not isinstance(enabled, bool):
            raise AppServerError("fast mode must be a boolean")
        cwd = self.store.current_cwd(channel_id, conversation_id)
        config_result = await self.client.read_config(include_layers=False, cwd=cwd)
        config_result.update(await self._read_config_requirements_if_supported())
        requirements = config_result.get("requirements")
        if self._managed_new_thread_value(requirements, "serviceTier", "service_tier") is not None:
            raise AppServerError("Fast mode is managed by Codex requirements")
        config = self._effective_config(config_result)
        if enabled:
            if not self._fast_feature_available(config, requirements):
                raise AppServerError("Fast mode is disabled by native Codex feature requirements")
            effective_config, _managed = self._global_effective_config(config, requirements)
            catalog = await self.list_models(include_hidden=True)
            models = [item for item in catalog.get("data", []) if isinstance(item, dict)]
            selected = self._select_reasoning_model(effective_config, models)
            if selected is None or not self._model_supports_fast_tier(selected):
                model = str(
                    (selected or {}).get("displayName")
                    or (selected or {}).get("id")
                    or self._configured_model_id(effective_config)
                    or "the active model"
                )
                raise AppServerError(f"Fast mode is not available for {model} in the native model catalog")
        return await self._write_user_config_edits(
            edits=[
                {
                    "keyPath": "service_tier",
                    "value": "priority" if enabled else "default",
                    "mergeStrategy": "replace",
                }
            ],
            reload_user_config=False,
            config_result=config_result,
        )

    async def call_native(self, method: str, params: dict | None = None) -> dict:
        return await self.client.call(method, params)

    async def _read_config_requirements_if_supported(self) -> dict:
        try:
            return await self.client.read_config_requirements()
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            return {}

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
        result = await self._write_user_config_edits(
            edits=edits,
            reload_user_config=True,
            config_result=config_result,
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

    def _permission_profile_id(self, mode: str) -> str:
        profile_id = PERMISSION_MODE_PROFILE_IDS.get(mode)
        if profile_id is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        return profile_id

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
        for key in (
            "defaultPermissions",
            "defaultPermissionProfile",
            "default_permissions",
        ):
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
        return await self._write_user_config_edits(
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
            config_result=config_result,
        )

    async def _write_user_config_edits(
        self,
        *,
        edits: list[dict],
        reload_user_config: bool,
        config_result: dict | None,
    ) -> dict:
        expected_version, file_path = self._user_config_write_target(config_result)
        return await self.batch_write_config(
            edits=edits,
            reload_user_config=reload_user_config,
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

    def _normalized_personality(self, personality: str | None) -> str | None:
        if not isinstance(personality, str):
            return personality
        normalized = personality.lower()
        if normalized == "default":
            return None
        if normalized not in _PERSONALITIES:
            raise AppServerError(f"unsupported personality: {normalized}")
        return normalized

    async def _validate_personality_compatibility(
        self,
        personality: str | None,
        config_result: dict,
    ) -> None:
        if personality is None:
            return
        config = self._effective_config(config_result)
        requirements = config_result.get("requirements")
        if not self._personality_feature_available(config, requirements):
            raise AppServerError("personality is disabled by native Codex feature requirements")
        effective_config, _managed = self._global_effective_config(
            config,
            requirements,
        )
        catalog = await self.list_models(include_hidden=True)
        selected = self._select_reasoning_model(
            effective_config,
            [item for item in catalog.get("data", []) if isinstance(item, dict)],
        )
        if selected is not None and selected.get("supportsPersonality") is False:
            model = str(selected.get("displayName") or selected.get("id") or "the active model")
            raise AppServerError(f"personality is not supported by {model}")

    async def _validate_model_compatibility(
        self,
        model: str | None,
        config_result: dict,
    ) -> None:
        config = self._effective_config(config_result)
        requirements = config_result.get("requirements")
        candidate, _managed = self._global_effective_config(config, requirements)
        for key in ("model", "modelId", "modelID"):
            candidate.pop(key, None)
        if model is not None:
            candidate["model"] = model

        effort = self._managed_new_thread_value(
            requirements,
            "modelReasoningEffort",
            "model_reasoning_effort",
        )
        if effort is None:
            effort = config.get("model_reasoning_effort") or config.get("reasoningEffort")
        service_tier = self._managed_new_thread_value(requirements, "serviceTier", "service_tier")
        if service_tier is None:
            service_tier = config.get("service_tier") or config.get("serviceTier")
        personality = str(config.get("personality") or "").strip().lower()
        personality_active = (
            self._personality_feature_available(config, requirements)
            and personality not in {"", "default"}
        )
        fast_requested = self._fast_feature_available(config, requirements) and str(
            service_tier or ""
        ).strip().lower() in {"fast", "priority"}
        if effort is None and not fast_requested and not personality_active:
            return

        catalog = await self.list_models(include_hidden=True)
        selected = self._select_reasoning_model(
            candidate,
            [item for item in catalog.get("data", []) if isinstance(item, dict)],
        )
        if selected is None:
            if model is not None:
                raise AppServerError(f"model {model} was not found in the native model catalog")
            return

        native_efforts = self._native_reasoning_efforts(selected)
        if effort is not None and native_efforts is not None:
            supported = {item["reasoningEffort"] for item in native_efforts}
            if str(effort).lower() not in supported:
                raise AppServerError(
                    f"the selected model does not support the configured reasoning effort {effort}"
                )

        if fast_requested and not self._model_supports_fast_tier(selected):
            raise AppServerError("the selected model does not support the configured Fast service tier")

        if personality_active and selected.get("supportsPersonality") is False:
            raise AppServerError("the selected model does not support the configured personality")

    def _effective_config(self, payload: dict) -> dict:
        config = payload.get("config")
        return config if isinstance(config, dict) else payload

    def _global_effective_config(
        self,
        config: dict,
        requirements: object,
    ) -> tuple[dict, list[str]]:
        effective = dict(config)
        managed: list[str] = []
        mappings = (
            ("model", ("model",), "model"),
            (
                "reasoningEffort",
                ("modelReasoningEffort", "model_reasoning_effort"),
                "model_reasoning_effort",
            ),
            ("fast", ("serviceTier", "service_tier"), "service_tier"),
        )
        for setting, requirement_keys, config_key in mappings:
            value = self._managed_new_thread_value(requirements, *requirement_keys)
            if value is None:
                continue
            effective[config_key] = value
            managed.append(setting)
        managed_permission = self._requirements_default_permission(requirements)
        if managed_permission is not None:
            effective["default_permissions"] = managed_permission
            managed.append("permissionMode")
        return effective, managed

    @staticmethod
    def _managed_new_thread_value(requirements: object, *keys: str) -> object | None:
        if not isinstance(requirements, dict):
            return None
        models = requirements.get("models")
        if not isinstance(models, dict):
            return None
        new_thread = models.get("newThread")
        if not isinstance(new_thread, dict):
            new_thread = models.get("new_thread")
        if not isinstance(new_thread, dict):
            return None
        for key in keys:
            if new_thread.get(key) is not None:
                return new_thread[key]
        return None

    @staticmethod
    def _fast_feature_available(config: dict, requirements: object) -> bool:
        features = config.get("features")
        if isinstance(features, dict):
            for key in ("fast_mode", "fastMode"):
                if key in features and features[key] is False:
                    return False
        if not isinstance(requirements, dict):
            return True
        feature_requirements = requirements.get("featureRequirements")
        if not isinstance(feature_requirements, dict):
            feature_requirements = requirements.get("feature_requirements")
        if not isinstance(feature_requirements, dict):
            return True
        for key in ("fast_mode", "fastMode"):
            if key not in feature_requirements:
                continue
            value = feature_requirements[key]
            if value is False:
                return False
            if isinstance(value, dict) and any(value.get(field) is False for field in ("enabled", "value", "required")):
                return False
        return True

    @staticmethod
    def _personality_feature_available(config: dict, requirements: object) -> bool:
        features = config.get("features")
        if isinstance(features, dict) and features.get("personality") is False:
            return False
        if not isinstance(requirements, dict):
            return True
        for container_key in ("features", "featureRequirements", "feature_requirements"):
            container = requirements.get(container_key)
            if not isinstance(container, dict) or "personality" not in container:
                continue
            value = container["personality"]
            if value is False:
                return False
            if isinstance(value, dict) and any(
                value.get(field) is False for field in ("enabled", "value", "required")
            ):
                return False
        return True

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

    @staticmethod
    def _model_supports_fast_tier(model: dict) -> bool:
        default_tier = str(model.get("defaultServiceTier") or "").strip().lower()
        if default_tier in {"fast", "priority"}:
            return True

        service_tiers = model.get("serviceTiers")
        if isinstance(service_tiers, list):
            for tier in service_tiers:
                if not isinstance(tier, dict):
                    continue
                tier_id = str(tier.get("id") or "").strip().lower()
                if tier_id in {"fast", "priority"}:
                    return True

        additional_tiers = model.get("additionalSpeedTiers")
        return isinstance(additional_tiers, list) and any(
            str(tier).strip().lower() in {"fast", "priority"} for tier in additional_tiers
        )

    def _validated_reasoning_effort(self, effort: str, options: dict) -> str:
        supported = {
            str(item.get("reasoningEffort") or "").lower()
            for item in options.get("reasoningEfforts", [])
            if isinstance(item, dict) and item.get("reasoningEffort")
        }
        normalized = effort.lower()
        if normalized not in supported:
            model = str(options.get("selectedModelDisplayName") or options.get("selectedModel") or "the active model")
            available = ", ".join(sorted(supported)) or "none"
            raise AppServerError(
                f"reasoning effort {normalized} is not supported by {model}; available efforts: {available}"
            )
        return normalized

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

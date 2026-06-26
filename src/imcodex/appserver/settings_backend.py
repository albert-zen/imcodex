from __future__ import annotations

from .client import AppServerError


PERMISSION_MODE_PROFILE_IDS = {
    "default": ":workspace",
    "read-only": ":read-only",
    "full-access": ":danger-full-access",
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


class CodexSettingsBackendMixin:
    async def list_models(self) -> dict:
        return await self.client.list_models()

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
        result = await self.read_config(channel_id, conversation_id)
        warnings: dict[str, str] = {}
        try:
            result["profiles"] = await self._list_permission_profiles(channel_id, conversation_id)
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

    async def set_permission_mode(self, channel_id: str, conversation_id: str, mode: str) -> dict:
        profile_id = PERMISSION_MODE_PROFILE_IDS.get(mode)
        if profile_id is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        try:
            options = await self.read_permission_options(channel_id, conversation_id)
        except AppServerError as exc:
            if not self._is_native_permission_profile_unsupported(exc):
                raise
            return await self._set_legacy_permission_mode(mode, warning=str(exc))
        if options.get("nativeProfilesSupported") is False:
            warning = str((options.get("warnings") or {}).get("profiles") or "")
            return await self._set_legacy_permission_mode(mode, warning=warning)
        if not self._permission_profile_is_available(profile_id, options):
            raise AppServerError(f"permission profile {profile_id} is not available in Codex")
        if not self._permission_profile_is_allowed(profile_id, options.get("requirements")):
            raise AppServerError(f"permission profile {profile_id} is not allowed by Codex requirements")
        write_result = await self.write_config_value(
            key_path="default_permissions",
            value=profile_id,
            merge_strategy="replace",
        )
        write_result["mode"] = mode
        write_result["profile"] = profile_id
        write_result["fallback"] = False
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
    ) -> dict:
        return await self.client.batch_write_config(
            edits=edits,
            reload_user_config=reload_user_config,
        )

    async def set_default_model(self, model: str | None) -> dict:
        return await self.write_config_value(key_path="model", value=model, merge_strategy="replace")

    async def call_native(self, method: str, params: dict | None = None) -> dict:
        return await self.client.call(method, params)

    async def _list_permission_profiles(self, channel_id: str, conversation_id: str) -> list[dict]:
        cwd = self.store.current_cwd(channel_id, conversation_id)
        cursor: str | None = None
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

    async def _set_legacy_permission_mode(self, mode: str, *, warning: str = "") -> dict:
        edits = _LEGACY_PERMISSION_PRESETS.get(mode)
        if edits is None:
            raise AppServerError(f"unsupported permission mode: {mode}")
        result = await self.batch_write_config(edits=edits, reload_user_config=False)
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

    def _permission_profile_is_allowed(self, profile_id: str, requirements: object) -> bool:
        if not isinstance(requirements, dict):
            return True
        allowed = requirements.get("allowedPermissionProfiles")
        if not isinstance(allowed, dict):
            return True
        return bool(allowed.get(profile_id))

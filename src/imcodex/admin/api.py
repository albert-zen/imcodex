from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..appserver import AppServerError
from .config_store import (
    ConfigConflictError,
    ConfigStore,
    ConfigStoreError,
    ConfigValidationError,
)
from .native import apply_global_setting, public_native_settings, public_write_result
from .security import AdminAccessGuard, generate_csrf_token


_ASSET_ROOT = Path(__file__).with_name("assets")
_ASSETS = {
    "admin.css": "text/css",
    "admin.js": "text/javascript",
    "logo.svg": "image/svg+xml",
    "logo-lockup.svg": "image/svg+xml",
    "logo-primary.svg": "image/svg+xml",
}
_SECTION_METADATA = {
    "runtime": (
        "Runtime",
        "Local process paths, logging, and the HTTP listener. Changes apply after restart.",
    ),
    "app_server": (
        "App Server",
        "How the bridge connects and reconnects to the independently owned Codex App Server.",
    ),
    "webhooks": (
        "Webhook gateway",
        "Optional generic inbound and outbound HTTP integration for a trusted local gateway.",
    ),
    "qq": ("QQ", "QQ bot credentials, admission, and message rendering."),
    "telegram": (
        "Telegram",
        "Telegram bot credentials, admission, and polling behavior.",
    ),
    "feishu": (
        "Feishu / Lark",
        "Feishu or Lark app credentials and conversation admission.",
    ),
    "weixin": (
        "Weixin",
        "Experimental Tencent iLink Weixin transport and owner admission.",
    ),
}


def install_admin_routes(
    app: FastAPI,
    runtime,
    *,
    config_store: ConfigStore | None = None,
    csrf_token: str | None = None,
) -> str:
    store = config_store or ConfigStore(Path.cwd() / ".env")
    token = csrf_token or generate_csrf_token()
    app.state.admin_config_store = store
    app.state.admin_csrf_token = token

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_index() -> FileResponse:
        return FileResponse(_ASSET_ROOT / "index.html", media_type="text/html")

    @app.get("/admin/assets/{asset_name}", include_in_schema=False)
    async def admin_asset(asset_name: str) -> FileResponse:
        media_type = _ASSETS.get(asset_name)
        if media_type is None:
            raise HTTPException(status_code=404, detail="Admin asset not found.")
        return FileResponse(_ASSET_ROOT / asset_name, media_type=media_type)

    @app.get("/admin/api/config", include_in_schema=False)
    async def read_admin_config() -> dict:
        try:
            snapshot = await asyncio.to_thread(store.read)
        except ConfigStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _config_payload(
            snapshot,
            csrf_token=token,
            restart_required=store.restart_required(snapshot),
        )

    @app.put("/admin/api/config", include_in_schema=False)
    async def write_admin_config(payload: dict) -> dict:
        allowed_keys = {"revision", "values", "secrets"}
        if set(payload) - allowed_keys:
            raise HTTPException(status_code=422, detail="Unsupported configuration payload fields.")
        revision = payload.get("revision")
        values = payload.get("values", {})
        secrets = payload.get("secrets", {})
        if not isinstance(revision, str) or not revision:
            raise HTTPException(status_code=422, detail="A configuration revision is required.")
        if not isinstance(values, dict) or not isinstance(secrets, dict):
            raise HTTPException(status_code=422, detail="values and secrets must be objects.")
        try:
            snapshot = await asyncio.to_thread(
                store.update,
                expected_revision=revision,
                values=values,
                secrets=secrets,
            )
        except ConfigConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "currentRevision": exc.current,
                },
            ) from exc
        except ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConfigStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _config_payload(
            snapshot,
            csrf_token=token,
            restart_required=store.restart_required(snapshot),
        )

    @app.get("/admin/api/native", include_in_schema=False)
    async def read_admin_native() -> dict:
        backend = runtime.service.backend
        try:
            payload = await backend.read_global_settings()
        except (AppServerError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Native Codex settings are temporarily unavailable.",
            ) from exc
        return public_native_settings(payload, csrf_token=token)

    @app.put("/admin/api/native", include_in_schema=False)
    async def write_admin_native(payload: dict) -> dict:
        if set(payload) != {"setting", "value"}:
            raise HTTPException(status_code=422, detail="setting and value are required.")
        setting = payload.get("setting")
        if not isinstance(setting, str):
            raise HTTPException(status_code=422, detail="setting must be a string.")
        try:
            result = await apply_global_setting(
                runtime.service.backend,
                setting=setting,
                value=payload.get("value"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AppServerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from exc
        return {
            "ok": True,
            "setting": setting,
            "restartRequired": False,
            "result": public_write_result(result),
        }

    app.add_middleware(AdminAccessGuard, csrf_token=token)
    return token


def _config_payload(snapshot, *, csrf_token: str, restart_required: bool) -> dict:
    fields = snapshot.to_dict().get("fields", [])
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        section = str(field.get("section") or "runtime")
        if section not in grouped:
            grouped[section] = []
            order.append(section)
        public_field = dict(field)
        if public_field.get("type") == "secret":
            public_field["secretConfigured"] = bool(public_field.get("configured"))
        grouped[section].append(public_field)
    sections = []
    for section in order:
        label, description = _SECTION_METADATA.get(
            section,
            (section.replace("_", " ").title(), "Bridge-owned configuration."),
        )
        sections.append(
            {
                "id": section,
                "label": label,
                "description": description,
                "fields": grouped[section],
            }
        )
    return {
        "revision": snapshot.revision,
        "csrfToken": csrf_token,
        "sections": sections,
        "restartRequired": restart_required,
    }

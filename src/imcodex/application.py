from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import ipaddress
import os
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, Request

from .admin.api import install_admin_routes
from .channels import create_app
from .composition import SettingsSource, build_runtime
from .config import Settings
from .debug_harness.api import install_debug_routes
from .delivery_api import install_delivery_route
from .observability.health import (
    BRIDGE_HEALTH_KIND,
    BRIDGE_INSTANCE_HEADER,
    BRIDGE_SHUTDOWN_PATH,
)
from .runtime import AppRuntime


def create_application(
    *,
    settings=None,
    runtime: AppRuntime | None = None,
    admin_config_store=None,
    settings_source: SettingsSource | None = None,
):
    if settings is None:
        if settings_source not in (None, "environment"):
            raise ValueError("Application-loaded Settings must use the environment source")
        settings = Settings.from_env()
        resolved_settings_source: SettingsSource = "environment"
    else:
        resolved_settings_source = settings_source or "explicit"
    runtime = runtime or build_runtime(
        settings,
        settings_source=resolved_settings_source,
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = runtime
        webhook_media = getattr(app.state, "webhook_media_materializer", None)
        webhook_files = getattr(app.state, "webhook_file_materializer", None)
        wait_for_webhook_form_cleanup = getattr(
            app.state,
            "wait_for_webhook_form_cleanup",
            None,
        )
        webhook_media_started = False
        webhook_files_started = False
        runtime_started = False
        credential_published = False
        delivery_credential = getattr(app.state, "delivery_credential", None)
        try:
            if webhook_media is not None:
                await webhook_media.start()
                webhook_media_started = True
            if webhook_files is not None:
                await webhook_files.start()
                webhook_files_started = True
            await runtime.start()
            runtime_started = True
            if delivery_credential is not None:
                delivery_credential.publish()
                credential_published = True
            yield
        finally:
            try:
                if runtime_started:
                    await runtime.stop()
            finally:
                try:
                    if credential_published and delivery_credential is not None:
                        delivery_credential.clear()
                finally:
                    try:
                        if webhook_files_started and webhook_files is not None:
                            await webhook_files.stop()
                    finally:
                        try:
                            if webhook_media_started and webhook_media is not None:
                                await webhook_media.stop()
                        finally:
                            if callable(wait_for_webhook_form_cleanup):
                                await wait_for_webhook_form_cleanup()

    app = create_app(
        runtime.service,
        inbound_token=str(getattr(settings, "inbound_webhook_token", "") or ""),
        media_dir=Path(getattr(settings, "data_dir", Path(".imcodex")))
        / "channels"
        / "webhook"
        / "inbound-media",
    )

    @app.get("/healthz", include_in_schema=False)
    async def bridge_health() -> dict[str, object]:
        context = getattr(getattr(runtime, "observability", None), "context", None)
        pid = int(getattr(context, "pid", os.getpid()))
        instance_id = str(getattr(context, "instance_id", "") or f"p{pid}")
        return {
            "kind": BRIDGE_HEALTH_KIND,
            "status": "healthy",
            "pid": pid,
            "instanceId": instance_id,
        }

    @app.post(BRIDGE_SHUTDOWN_PATH, include_in_schema=False, status_code=202)
    async def graceful_bridge_shutdown(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict[str, str]:
        client_host = str(request.client.host if request.client is not None else "")
        try:
            address = ipaddress.ip_address(client_host)
            loopback = address.is_loopback or bool(
                getattr(address, "ipv4_mapped", None)
                and address.ipv4_mapped.is_loopback
            )
        except ValueError:
            loopback = False
        context = getattr(getattr(runtime, "observability", None), "context", None)
        instance_id = str(getattr(context, "instance_id", "") or "")
        supplied_instance = request.headers.get(BRIDGE_INSTANCE_HEADER, "")
        if not loopback or not instance_id or not hmac.compare_digest(
            supplied_instance,
            instance_id,
        ):
            raise HTTPException(status_code=403, detail="Graceful shutdown request was rejected.")
        request_shutdown = getattr(app.state, "request_shutdown", None)
        if not callable(request_shutdown):
            raise HTTPException(status_code=503, detail="Graceful shutdown is unavailable.")
        background_tasks.add_task(request_shutdown)
        return {"status": "shutting_down"}

    install_admin_routes(app, runtime, config_store=admin_config_store)
    app.state.delivery_credential = install_delivery_route(
        app,
        runtime,
        data_dir=Path(getattr(settings, "data_dir", Path(".imcodex"))),
        run_dir=Path(getattr(settings, "run_dir", Path(".imcodex-run"))),
    )
    if bool(getattr(settings, "debug_api_enabled", False)):
        install_debug_routes(app, runtime)
    app.router.lifespan_context = lifespan
    return app

from __future__ import annotations
from contextlib import asynccontextmanager

from .channels import create_app
from .composition import build_runtime
from .debug_harness.api import install_debug_routes
from .runtime import AppRuntime


def create_application(
    *,
    settings=None,
    runtime: AppRuntime | None = None,
):
    runtime = runtime or build_runtime(settings)

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = create_app(runtime.service)
    if bool(getattr(settings, "debug_api_enabled", False)):
        install_debug_routes(app, runtime)
    app.router.lifespan_context = lifespan
    return app

from __future__ import annotations

from contextlib import asynccontextmanager
import time

import websockets

from .api import create_app
from .appserver_client import AppServerClient
from .appserver_supervisor import AppServerSupervisor
from .backend import CodexBackend
from .commands import CommandRouter
from .config import Settings
from .outbound import WebhookOutboundSink
from .projector import MessageProjector
from .runtime import AppRuntime
from .service import BridgeService
from .store import ConversationStore


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    settings = settings or Settings.from_env()
    store = ConversationStore(clock=time.time, state_path=settings.data_dir / "state.json")
    client = AppServerClient(
        websocket_factory=websockets.connect,
        transport_url=settings.app_server_ws_url,
        client_info={
            "name": settings.service_name,
            "title": "IM Codex Bridge",
            "version": "0.1.0",
        },
    )
    supervisor = AppServerSupervisor(
        port=settings.app_server_port,
        codex_bin=settings.codex_bin,
        host=settings.app_server_host,
    )
    outbound_sink = (
        WebhookOutboundSink(settings.outbound_url) if settings.outbound_url else None
    )
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name=settings.service_name),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=outbound_sink,
    )
    return AppRuntime(supervisor=supervisor, client=client, service=service)


def create_application(
    *,
    settings: Settings | None = None,
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
    app.router.lifespan_context = lifespan
    return app

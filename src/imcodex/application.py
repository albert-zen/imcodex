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
from .outbound import MultiplexOutboundSink, WebhookOutboundSink
from .projector import MessageProjector
from .qq_adapter import QQChannelAdapter
from .runtime import AppRuntime
from .service import BridgeService
from .store import ConversationStore


async def open_blocking_websocket(url: str):
    return await websockets.connect(url, open_timeout=10)


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    settings = settings or Settings.from_env()
    store = ConversationStore(clock=time.time, state_path=settings.data_dir / "state.json")
    client = AppServerClient(
        websocket_factory=open_blocking_websocket,
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
    default_outbound_sink = (
        WebhookOutboundSink(settings.outbound_url) if settings.outbound_url else None
    )
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name=settings.service_name),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=None,
        auto_approve_mode=_resolve_auto_approve_mode(settings),
    )
    managed_channels = []
    channel_sinks = {}
    if settings.qq_enabled:
        qq_adapter = QQChannelAdapter(
            enabled=True,
            app_id=settings.qq_app_id,
            client_secret=settings.qq_client_secret,
            service=service,
            api_base=settings.qq_api_base,
        )
        managed_channels.append(qq_adapter)
        channel_sinks["qq"] = qq_adapter
    if channel_sinks or default_outbound_sink is not None:
        service.outbound_sink = MultiplexOutboundSink(
            channel_sinks=channel_sinks,
            default_sink=default_outbound_sink,
        )
    return AppRuntime(
        supervisor=supervisor,
        client=client,
        service=service,
        managed_channels=managed_channels,
    )


def _resolve_auto_approve_mode(settings: Settings) -> str | None:
    if not settings.auto_approve:
        return None
    if settings.auto_approve_mode.lower() in {"session", "acceptforsession"}:
        return "acceptForSession"
    return "accept"


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

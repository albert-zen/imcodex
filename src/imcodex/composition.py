from __future__ import annotations

import time
from pathlib import Path

from .appserver import AppServerClient, AppServerSupervisor, CodexBackend
from .bridge import BridgeService, CommandRouter, MessageProjector
from .channels import MultiplexOutboundSink, UnifiedChannelMiddleware, WebhookOutboundSink
from .channels.registry import build_enabled_channel_adapters
from .config import Settings
from .observability.runtime import ObservabilityRuntime
from .runtime import AppRuntime
from .store import ConversationStore


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    settings = settings or Settings.from_env()
    store = ConversationStore(state_path=settings.data_dir / "state.json", clock=time.time)
    supervisor = AppServerSupervisor(
        codex_bin=settings.codex_bin,
        app_server_url=settings.app_server_url,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": settings.service_name, "title": "IM Codex Bridge", "version": "0.1.0"},
    )
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name=settings.service_name),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=None,
    )
    channel_middleware = UnifiedChannelMiddleware(service=service)
    default_outbound_sink = WebhookOutboundSink(settings.outbound_url) if settings.outbound_url else None
    managed_channels = build_enabled_channel_adapters(settings=settings, middleware=channel_middleware)
    channel_sinks = {channel.channel_id: channel for channel in managed_channels}
    if channel_sinks or default_outbound_sink is not None:
        service.outbound_sink = MultiplexOutboundSink(channel_sinks=channel_sinks, default_sink=default_outbound_sink)
    observability = ObservabilityRuntime(
        run_root=settings.run_dir,
        service_name=settings.service_name,
        log_level=settings.log_level,
        http_host=settings.http_host,
        http_port=settings.http_port,
        app_server_url=settings.app_server_url,
        cwd=Path.cwd(),
    )
    return AppRuntime(
        client=client,
        service=service,
        managed_channels=managed_channels,
        observability=observability,
    )

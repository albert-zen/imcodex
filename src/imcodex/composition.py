from __future__ import annotations

import time

from .appserver import AppServerClient, AppServerSupervisor, CodexBackend
from .bridge import BridgeService, CommandRouter, MessageProjector
from .channels import MultiplexOutboundSink, QQChannelAdapter, WebhookOutboundSink
from .config import Settings
from .logging_utils import configure_logging
from .runtime import AppRuntime
from .store import ConversationStore


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
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
    default_outbound_sink = WebhookOutboundSink(settings.outbound_url) if settings.outbound_url else None
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
        service.outbound_sink = MultiplexOutboundSink(channel_sinks=channel_sinks, default_sink=default_outbound_sink)
    return AppRuntime(client=client, service=service, managed_channels=managed_channels)

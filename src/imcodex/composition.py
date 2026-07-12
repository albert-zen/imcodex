from __future__ import annotations

import time
from pathlib import Path

from .appserver import AppServerClient, AppServerSupervisor, CodexBackend
from .appserver.retry import RetryBackoff
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
    retry_backoff = RetryBackoff(
        initial_delay_s=settings.app_server_retry_initial_delay_s,
        max_delay_s=settings.app_server_retry_max_delay_s,
        jitter_fraction=settings.app_server_retry_jitter_fraction,
    )
    supervisor = AppServerSupervisor(
        codex_bin=settings.codex_bin,
        app_server_url=settings.app_server_url,
        core_mode=settings.core_mode,
        core_url=settings.core_url,
        app_server_auth_token=settings.app_server_auth_token,
        app_server_auth_token_file=settings.app_server_auth_token_file,
        websocket_retry_policy=retry_backoff.with_max_attempts(settings.app_server_connect_max_attempts),
        websocket_open_timeout_s=settings.app_server_connect_timeout_s,
        health_probe_timeout_s=settings.app_server_health_timeout_s,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={"name": settings.service_name, "title": "IM Codex Bridge", "version": "0.1.0"},
        experimental_api_enabled=settings.app_server_experimental_api_enabled,
        request_retry_policy=retry_backoff.with_max_attempts(settings.app_server_request_max_attempts),
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
        app_server_url=settings.core_url or settings.app_server_url,
        cwd=Path.cwd(),
    )
    observability.write_launch_snapshot(
        command=["python", "-m", "imcodex"],
        cwd=Path.cwd(),
        env={
            "IMCODEX_DATA_DIR": str(settings.data_dir),
            "IMCODEX_RUN_DIR": str(settings.run_dir),
            "IMCODEX_HTTP_HOST": settings.http_host,
            "IMCODEX_HTTP_PORT": str(settings.http_port),
            "IMCODEX_CODEX_BIN": settings.codex_bin,
            "IMCODEX_APP_SERVER_EXPERIMENTAL_API": "1" if settings.app_server_experimental_api_enabled else "0",
            "IMCODEX_CORE_MODE": settings.core_mode,
            "IMCODEX_CORE_URL": settings.core_url or "",
            "IMCODEX_APP_SERVER_AUTH_TOKEN_FILE": str(settings.app_server_auth_token_file or ""),
            "IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS": str(settings.app_server_connect_max_attempts),
            "IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS": str(settings.app_server_request_max_attempts),
            "IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY": str(settings.app_server_retry_initial_delay_s),
            "IMCODEX_APP_SERVER_RETRY_MAX_DELAY": str(settings.app_server_retry_max_delay_s),
            "IMCODEX_APP_SERVER_RETRY_JITTER": str(settings.app_server_retry_jitter_fraction),
            "IMCODEX_APP_SERVER_CONNECT_TIMEOUT": str(settings.app_server_connect_timeout_s),
            "IMCODEX_APP_SERVER_HEALTH_TIMEOUT": str(settings.app_server_health_timeout_s),
            "IMCODEX_RESTART_EXECUTOR": settings.restart_executor or "",
            "IMCODEX_DEBUG_API_ENABLED": "1" if settings.debug_api_enabled else "0",
            "IMCODEX_QQ_ENABLED": "1" if settings.qq_enabled else "0",
            "IMCODEX_QQ_APP_ID": settings.qq_app_id,
            "IMCODEX_QQ_API_BASE": settings.qq_api_base,
            "IMCODEX_QQ_MARKDOWN_ENABLED": "1" if settings.qq_markdown_enabled else "0",
            "IMCODEX_TELEGRAM_ENABLED": "1" if settings.telegram_enabled else "0",
            "IMCODEX_TELEGRAM_API_BASE": settings.telegram_api_base,
            "IMCODEX_TELEGRAM_REQUIRE_MENTION": "1" if settings.telegram_require_mention else "0",
            "IMCODEX_TELEGRAM_POLL_TIMEOUT": str(settings.telegram_poll_timeout_s),
            "IMCODEX_FEISHU_ENABLED": "1" if settings.feishu_enabled else "0",
            "IMCODEX_FEISHU_APP_ID": settings.feishu_app_id,
            "IMCODEX_FEISHU_DOMAIN": settings.feishu_domain,
            "IMCODEX_FEISHU_REQUIRE_MENTION": "1" if settings.feishu_require_mention else "0",
            "IMCODEX_FEISHU_STARTUP_TIMEOUT": str(settings.feishu_startup_timeout_s),
        },
    )
    return AppRuntime(
        client=client,
        service=service,
        managed_channels=managed_channels,
        observability=observability,
    )

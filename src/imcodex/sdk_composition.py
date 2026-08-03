from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from imagent.applications import CodexApplicationAdapter
from imagent.applications.appserver_client import AppServerClient, AppServerSupervisor
from imagent.applications.appserver_client.retry import RetryBackoff
from imagent.channels import channel_from_config
from imagent.gateway import GatewayExtensions, GatewayRepositories, ImAgentGateway
from imagent.proactive_delivery import ScopedDeliveryAuthorizer
from imagent.storage import SQLiteGatewayState

from .appserver import CodexBackend
from .bridge import CommandRouter, ImcodexCommandPolicy
from .bridge.outbound_artifacts import (
    OutboundArtifactLeaseLedger,
    OutboundArtifactStager,
)
from .bridge.sdk_controller import ImcodexController
from .bridge.sdk_delivery import (
    ImcodexDeliveryOutcomeObserver,
    ImcodexProactiveDelivery,
)
from .bridge.sdk_presentation import (
    ImcodexArtifactMaterializer,
    ImcodexCodexLiveActivityPresenter,
    ImcodexOutboundPresentation,
)
from .bridge.sdk_requests import ImcodexRequestPresenter
from .channels.outbound import WebhookOutboundSink
from .channels.registry import BUILTIN_CHANNEL_IDS
from .channels.sdk_webhook import ImcodexRuntimeService, SdkWebhookChannel
from .composition import _managed_core_shared_filesystem_verifier
from .sdk_migration import migrate_legacy_gateway_state, recover_legacy_deliveries
from .store import ConversationStore

SDK_APPLICATION_INSTANCE_ID = "codex-main"


@dataclass(frozen=True, slots=True)
class SdkComposition:
    gateway: ImAgentGateway
    state: SQLiteGatewayState
    controller: ImcodexController
    application: CodexApplicationAdapter
    channels: tuple[object, ...]
    client: AppServerClient
    product_store: ConversationStore
    service: ImcodexRuntimeService
    delivery_authorizer: ScopedDeliveryAuthorizer

    async def prepare(self) -> None:
        await migrate_legacy_gateway_state(
            product_store=self.product_store,
            gateway_state=self.state,
            application_instance_id=SDK_APPLICATION_INSTANCE_ID,
            native_channel_ids=frozenset(BUILTIN_CHANNEL_IDS),
        )
        await self.service.delivery_service.artifact_ledger.reconcile(self.state)

    async def recover_legacy_deliveries(self) -> int:
        completed = await recover_legacy_deliveries(
            product_store=self.product_store,
            delivery_service=self.service.delivery_service,
        )
        self.service.delivery_service.artifact_ledger.cleanup()
        return completed


def build_sdk_composition(settings) -> SdkComposition:
    """Build the target SDK-owned runtime graph without starting external I/O."""

    if settings.native_thread_tool_host:
        raise RuntimeError(
            "IMCODEX_NATIVE_THREAD_TOOL_HOST is unavailable in the SDK-owned runtime; "
            "a typed Application operation is required before native tool hosting can be enabled"
        )

    target = settings.app_server_target
    product_store = ConversationStore(
        state_path=settings.data_dir / "state.json",
        clock=time.time,
    )
    retry = RetryBackoff(
        initial_delay_s=settings.app_server_retry_initial_delay_s,
        max_delay_s=settings.app_server_retry_max_delay_s,
        jitter_fraction=settings.app_server_retry_jitter_fraction,
    )
    supervisor = AppServerSupervisor(
        codex_bin=settings.codex_bin,
        app_server_url=target.endpoint,
        app_server_auth_token=settings.app_server_auth_token,
        app_server_auth_token_file=settings.app_server_auth_token_file,
        websocket_retry_policy=retry.with_max_attempts(
            settings.app_server_connect_max_attempts
        ),
        websocket_open_timeout_s=settings.app_server_connect_timeout_s,
        health_probe_timeout_s=settings.app_server_health_timeout_s,
    )
    shared_verifier = _managed_core_shared_filesystem_verifier(
        settings=settings,
        endpoint=target.endpoint,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={
            "name": settings.service_name,
            "title": "IM Codex Bridge",
            "version": "0.1.0",
        },
        experimental_api_enabled=(
            settings.app_server_experimental_api_enabled or not target.is_external
        ),
        shared_filesystem_verifier=shared_verifier,
        request_retry_policy=retry.with_max_attempts(
            settings.app_server_request_max_attempts
        ),
        reconnect_retry_policy=RetryBackoff(
            initial_delay_s=settings.app_server_reconnect_initial_delay_s,
            max_delay_s=settings.app_server_reconnect_max_delay_s,
            jitter_fraction=settings.app_server_reconnect_jitter_fraction,
        ),
    )
    backend = CodexBackend(
        client=client,
        store=product_store,
        service_name=settings.service_name,
    )
    client.add_connection_ready_handler(backend.ensure_default_permission_mode)
    artifact_stager = OutboundArtifactStager(settings.data_dir / "outbound-media")
    artifact_ledger = OutboundArtifactLeaseLedger(
        stager=artifact_stager,
        product_store=product_store,
        state_path=settings.data_dir / "outbound-artifact-leases.json",
    )
    artifact_ledger.cleanup()
    command_service = ImcodexCommandPolicy(
        store=product_store,
        backend=backend,
        command_router=CommandRouter(product_store),
    )
    request_presenter = ImcodexRequestPresenter()
    controller = ImcodexController(
        service=command_service,
        request_presenter=request_presenter,
        application_instance_id=SDK_APPLICATION_INSTANCE_ID,
    )
    shared_root = _shared_filesystem_root(target=target, verifier=shared_verifier)
    application = CodexApplicationAdapter(
        application_instance_id=SDK_APPLICATION_INSTANCE_ID,
        client=client,
        cwd=str(Path.cwd()),
        shared_filesystem_root=shared_root,
        live_activity_presenter=ImcodexCodexLiveActivityPresenter(),
        artifact_materializer=ImcodexArtifactMaterializer(
            artifact_stager=artifact_stager,
        ),
    )
    outbound_sink = (
        WebhookOutboundSink(
            settings.outbound_url,
            bearer_token=settings.outbound_webhook_token,
            outbound_media_dir=settings.data_dir / "outbound-media",
        )
        if settings.outbound_url
        else None
    )
    webhook_channel = SdkWebhookChannel(outbound_sink=outbound_sink)
    channels = (webhook_channel, *build_sdk_managed_channels(settings))
    state = SQLiteGatewayState(settings.data_dir / "gateway.sqlite3")
    delivery_authorizer = ScopedDeliveryAuthorizer()
    gateway = ImAgentGateway(
        channels=list(channels),
        applications=[application],
        repositories=GatewayRepositories(
            bindings=state,
            idempotency=state,
            delivery_submissions=state,
            projections=state,
            request_correlations=state,
        ),
        extensions=GatewayExtensions(
            controller=controller,
            request_presenter=request_presenter,
            delivery_outcome_observer=ImcodexDeliveryOutcomeObserver(
                artifact_ledger=artifact_ledger,
            ),
            outbound_presentation=ImcodexOutboundPresentation(
                store=product_store,
                artifact_ledger=artifact_ledger,
            ),
        ),
        delivery_authorizer=delivery_authorizer,
    )
    composition = SdkComposition(
        gateway=gateway,
        state=state,
        controller=controller,
        application=application,
        channels=channels,
        client=client,
        product_store=product_store,
        service=ImcodexRuntimeService(
            channel=webhook_channel,
            product_service=command_service,
        ),
        delivery_authorizer=delivery_authorizer,
    )
    composition.service.delivery_service = ImcodexProactiveDelivery(
        gateway=gateway,
        authorizer=delivery_authorizer,
        product_store=product_store,
        artifact_ledger=artifact_ledger,
        registered_channel_ids={
            channel.channel_instance_id
            for channel in channels
            if channel is not webhook_channel
        },
        fallback_excluded_channel_ids=set(BUILTIN_CHANNEL_IDS),
        webhook_channel=webhook_channel,
    )
    return composition


def build_sdk_managed_channels(settings) -> tuple[object, ...]:
    """Construct only enabled native Channel wrappers for pure preflight validation."""

    return tuple(
        channel_from_config(channel_id, config=config)
        for channel_id, config in settings.channel_configs().items()
        if bool(config.get("enabled"))
    )


def _shared_filesystem_root(*, target, verifier) -> Path | None:
    if (
        target.transport == "unix-websocket"
        or not target.is_external
        or verifier is not None
    ):
        anchor = Path.cwd().anchor
        if os.name == "nt" and not anchor:
            return None
        return Path(anchor or "/")
    return None

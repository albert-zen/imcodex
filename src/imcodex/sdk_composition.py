from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from imagent import (
    Gateway,
    GatewayExtensions,
    ProjectionPolicy,
    SQLiteGatewayStore,
)
from imagent.applications import CodexApplicationAdapter, codex_app_server_client
from imagent.channels import channel_from_config
from imagent.gateway.delivery import ScopedDeliveryAuthorizer

from .bridge.sdk_controller import ImcodexController
from .bridge.sdk_presentation import ImcodexOutboundPresentation
from .bridge.sdk_requests import ImcodexRequestPresenter
from .delivery_artifacts import DeliveryArtifactStager
from .channels.outbound import WebhookOutboundSink
from .channels.sdk_webhook import SdkRuntimeService, SdkWebhookChannel
from .product_state import ProductState


SDK_APPLICATION_INSTANCE_ID = "codex-main"
SDK_WORKSPACE_ID = "imcodex-workspace"


@dataclass(frozen=True, slots=True)
class SdkComposition:
    gateway: Gateway
    state: SQLiteGatewayStore
    controller: ImcodexController
    application: CodexApplicationAdapter
    channels: tuple[object, ...]
    client: object
    product_state: ProductState
    presentation: ImcodexOutboundPresentation
    request_presenter: ImcodexRequestPresenter
    delivery_authorizer: ScopedDeliveryAuthorizer
    artifact_stager: DeliveryArtifactStager
    service: SdkRuntimeService


def build_sdk_composition(settings) -> SdkComposition:
    """Build the production graph through the installed public SDK v1 API."""

    if settings.native_thread_tool_host:
        raise RuntimeError(
            "IMCODEX_NATIVE_THREAD_TOOL_HOST is unavailable through public SDK v1; "
            "a typed Application capability is required before enabling it"
        )

    target = settings.app_server_target
    product_state = ProductState(settings.data_dir / "product.json")
    client = codex_app_server_client(
        codex_bin=settings.codex_bin,
        endpoint=target.endpoint,
        auth_token=settings.app_server_auth_token,
        auth_token_file=settings.app_server_auth_token_file,
        experimental_api_enabled=(
            settings.app_server_experimental_api_enabled or not target.is_external
        ),
    )
    application = CodexApplicationAdapter(
        application_instance_id=SDK_APPLICATION_INSTANCE_ID,
        client=client,
        workspace_id=SDK_WORKSPACE_ID,
        cwd=str(Path.cwd()),
        shared_filesystem_root=_shared_filesystem_root(target),
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
    webhook = SdkWebhookChannel(outbound_sink=outbound_sink)
    native_channels = build_sdk_managed_channels(settings)
    channels = (webhook, *native_channels)
    state = SQLiteGatewayStore(settings.data_dir / "gateway.sqlite3")
    delivery_authorizer = ScopedDeliveryAuthorizer()
    artifact_stager = DeliveryArtifactStager(settings.data_dir / "outbound-media")
    request_presenter = ImcodexRequestPresenter()
    presentation = ImcodexOutboundPresentation(product_state=product_state)
    controller = ImcodexController(
        client=client,
        product_state=product_state,
        request_presenter=request_presenter,
        application_instance_id=SDK_APPLICATION_INSTANCE_ID,
        workspace_id=SDK_WORKSPACE_ID,
    )
    gateway = Gateway(
        gateway_id="imcodex",
        channels=list(channels),
        applications=[application],
        store=state,
        controller=controller,
        projection_policy=ProjectionPolicy.FOREGROUND_ONLY,
        extensions=GatewayExtensions(
            request_presenter=request_presenter,
            outbound_presentation=presentation,
        ),
        delivery_authorizer=delivery_authorizer,
    )
    service = SdkRuntimeService(
        channel=webhook,
        gateway=gateway,
        client=client,
        product_state=product_state,
        channels=channels,
        delivery_authorizer=delivery_authorizer,
        artifact_stager=artifact_stager,
    )
    return SdkComposition(
        gateway=gateway,
        state=state,
        controller=controller,
        application=application,
        channels=channels,
        client=client,
        product_state=product_state,
        presentation=presentation,
        request_presenter=request_presenter,
        delivery_authorizer=delivery_authorizer,
        artifact_stager=artifact_stager,
        service=service,
    )


def build_sdk_managed_channels(settings) -> tuple[object, ...]:
    """Construct enabled SDK-native transports for preflight and startup."""

    return tuple(
        channel_from_config(
            channel_id,
            config=dict(config),
            channel_instance_id=channel_id,
        )
        for channel_id, config in settings.channel_configs().items()
        if bool(config.get("enabled"))
    )


def _shared_filesystem_root(target) -> Path | None:
    if target.transport in {"stdio-jsonl", "unix-websocket"}:
        anchor = Path.cwd().anchor
        if os.name == "nt" and not anchor:
            return None
        return Path(anchor or "/")
    return None

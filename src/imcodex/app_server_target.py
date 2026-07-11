"""Canonical App Server target model shared by config and protocol layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit


DEFAULT_APP_SERVER_ENDPOINT = "unix://"
LEGACY_WEBSOCKET_ENDPOINT = "ws://127.0.0.1:8765"
STDIO_APP_SERVER_ENDPOINT = "stdio://"
EXTERNAL_CONNECTION_MODE = "external"
SPAWNED_STDIO_CONNECTION_MODE = "spawned-stdio"

AppServerOwnership = Literal["external", "bridge-child"]
AppServerTransportKind = Literal["unix-websocket", "tcp-websocket", "stdio-jsonl"]


class AppServerTargetConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppServerTarget:
    endpoint: str
    ownership: AppServerOwnership
    transport: AppServerTransportKind

    @property
    def is_external(self) -> bool:
        return self.ownership == "external"

    @property
    def preserves_server_state(self) -> bool:
        return self.is_external

    @property
    def connection_mode(self) -> str:
        return EXTERNAL_CONNECTION_MODE if self.is_external else SPAWNED_STDIO_CONNECTION_MODE


def parse_app_server_target(endpoint: str) -> AppServerTarget:
    normalized = str(endpoint or "").strip()
    if normalized == STDIO_APP_SERVER_ENDPOINT:
        return AppServerTarget(
            endpoint=normalized,
            ownership="bridge-child",
            transport="stdio-jsonl",
        )
    if normalized.startswith("unix://"):
        return AppServerTarget(
            endpoint=normalized,
            ownership="external",
            transport="unix-websocket",
        )
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() in {"ws", "wss"} and parsed.netloc:
        return AppServerTarget(
            endpoint=normalized,
            ownership="external",
            transport="tcp-websocket",
        )
    raise AppServerTargetConfigError(
        "IMCODEX_APP_SERVER_URL must use unix://, ws://, wss://, or stdio://"
    )


def resolve_app_server_target(
    *,
    app_server_url: str | None = None,
    core_url: str | None = None,
    core_mode: str | None = None,
) -> AppServerTarget:
    canonical_url = str(app_server_url or "").strip() or None
    legacy_url = str(core_url or "").strip() or None
    if canonical_url and legacy_url and canonical_url != legacy_url:
        raise AppServerTargetConfigError(
            "IMCODEX_APP_SERVER_URL and legacy IMCODEX_CORE_URL disagree; configure only one endpoint"
        )

    requested_mode = str(core_mode or "").strip().lower() or None
    if requested_mode == "auto":
        raise AppServerTargetConfigError(
            "IMCODEX_CORE_MODE=auto is no longer supported because it silently changes App Server "
            "lifecycle; remove it and configure IMCODEX_APP_SERVER_URL explicitly"
        )
    external_aliases = {"external", "dedicated-ws", "shared-ws"}
    stdio_aliases = {"stdio", "spawned-stdio"}
    if requested_mode not in external_aliases | stdio_aliases | {None}:
        raise AppServerTargetConfigError(
            f"unsupported legacy IMCODEX_CORE_MODE: {requested_mode}; "
            "configure IMCODEX_APP_SERVER_URL instead"
        )

    endpoint = canonical_url or legacy_url
    if endpoint is None:
        if requested_mode in {"dedicated-ws", "shared-ws"}:
            endpoint = LEGACY_WEBSOCKET_ENDPOINT
        elif requested_mode in stdio_aliases:
            endpoint = STDIO_APP_SERVER_ENDPOINT
        else:
            endpoint = DEFAULT_APP_SERVER_ENDPOINT

    target = parse_app_server_target(endpoint)
    if requested_mode in external_aliases and not target.is_external:
        raise AppServerTargetConfigError(
            f"legacy mode {requested_mode} conflicts with App Server endpoint {target.endpoint}"
        )
    if requested_mode in stdio_aliases and target.is_external:
        raise AppServerTargetConfigError(
            f"legacy mode {requested_mode} conflicts with external App Server endpoint {target.endpoint}"
        )
    return target

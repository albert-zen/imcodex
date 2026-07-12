from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Literal

from .appserver import AppServerClient, AppServerSupervisor, CodexBackend
from .appserver.retry import RetryBackoff
from .appserver.supervisor import resolve_unix_socket_path
from .bridge import BridgeService, CommandRouter, MessageProjector
from .channels import (
    MultiplexOutboundSink,
    UnifiedChannelMiddleware,
    WebhookOutboundSink,
)
from .channels.registry import build_enabled_channel_adapters
from .config import (
    DOTENV_IMPORTED_KEYS_ENV,
    KNOWN_SETTING_ENV_KEYS,
    LAUNCHER_RELOADABLE_KEYS_ENV,
    PREFLIGHT_CURRENT_HTTP_HOST_ENV,
    PREFLIGHT_CURRENT_HTTP_PORT_ENV,
    TARGET_ENVIRONMENT_KEYS,
    Settings,
    is_restart_context_env_key,
)
from .observability.runtime import ObservabilityRuntime
from .runtime import AppRuntime
from .store import ConversationStore


SettingsSource = Literal["environment", "explicit"]


def build_runtime(
    settings: Settings | None = None,
    *,
    settings_source: SettingsSource | None = None,
) -> AppRuntime:
    if settings is None:
        if settings_source not in (None, "environment"):
            raise ValueError("Settings loaded by build_runtime must use the environment source")
        settings = Settings.from_env()
        resolved_settings_source: SettingsSource = "environment"
    else:
        resolved_settings_source = settings_source or "explicit"
    if resolved_settings_source not in {"environment", "explicit"}:
        raise ValueError(f"Unsupported settings source: {resolved_settings_source}")
    app_server_target = settings.app_server_target
    store = ConversationStore(state_path=settings.data_dir / "state.json", clock=time.time)
    retry_backoff = RetryBackoff(
        initial_delay_s=settings.app_server_retry_initial_delay_s,
        max_delay_s=settings.app_server_retry_max_delay_s,
        jitter_fraction=settings.app_server_retry_jitter_fraction,
    )
    supervisor = AppServerSupervisor(
        codex_bin=settings.codex_bin,
        app_server_url=app_server_target.endpoint,
        app_server_auth_token=settings.app_server_auth_token,
        app_server_auth_token_file=settings.app_server_auth_token_file,
        websocket_retry_policy=retry_backoff.with_max_attempts(settings.app_server_connect_max_attempts),
        websocket_open_timeout_s=settings.app_server_connect_timeout_s,
        health_probe_timeout_s=settings.app_server_health_timeout_s,
    )
    client = AppServerClient(
        supervisor=supervisor,
        client_info={
            "name": settings.service_name,
            "title": "IM Codex Bridge",
            "version": "0.1.0",
        },
        experimental_api_enabled=settings.app_server_experimental_api_enabled,
        request_retry_policy=retry_backoff.with_max_attempts(settings.app_server_request_max_attempts),
        reconnect_retry_policy=RetryBackoff(
            initial_delay_s=settings.app_server_reconnect_initial_delay_s,
            max_delay_s=settings.app_server_reconnect_max_delay_s,
            jitter_fraction=settings.app_server_reconnect_jitter_fraction,
        ),
    )
    service = BridgeService(
        store=store,
        backend=CodexBackend(client=client, store=store, service_name=settings.service_name),
        command_router=CommandRouter(store),
        projector=MessageProjector(),
        outbound_sink=None,
    )
    channel_middleware = UnifiedChannelMiddleware(service=service)
    default_outbound_sink = (
        WebhookOutboundSink(
            settings.outbound_url,
            bearer_token=settings.outbound_webhook_token,
        )
        if settings.outbound_url
        else None
    )
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
        app_server_url=app_server_target.endpoint,
        cwd=Path.cwd(),
    )
    launch_env = {
        "IMCODEX_DATA_DIR": str(settings.data_dir),
        "IMCODEX_RUN_DIR": str(settings.run_dir),
        "IMCODEX_HTTP_HOST": settings.http_host,
        "IMCODEX_HTTP_PORT": str(settings.http_port),
        "IMCODEX_CODEX_BIN": settings.codex_bin,
        "IMCODEX_APP_SERVER_EXPERIMENTAL_API": "1" if settings.app_server_experimental_api_enabled else "0",
        "IMCODEX_APP_SERVER_URL": app_server_target.endpoint,
        # Canonical target configuration is sufficient for restart. Keep
        # legacy aliases empty instead of regenerating a runtime mode that
        # was already normalized away.
        "IMCODEX_CORE_MODE": "",
        "IMCODEX_CORE_URL": "",
        "IMCODEX_APP_SERVER_AUTH_TOKEN_FILE": str(settings.app_server_auth_token_file or ""),
        "IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS": str(settings.app_server_connect_max_attempts),
        "IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS": str(settings.app_server_request_max_attempts),
        "IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY": str(settings.app_server_retry_initial_delay_s),
        "IMCODEX_APP_SERVER_RETRY_MAX_DELAY": str(settings.app_server_retry_max_delay_s),
        "IMCODEX_APP_SERVER_RETRY_JITTER": str(settings.app_server_retry_jitter_fraction),
        "IMCODEX_APP_SERVER_CONNECT_TIMEOUT": str(settings.app_server_connect_timeout_s),
        "IMCODEX_APP_SERVER_HEALTH_TIMEOUT": str(settings.app_server_health_timeout_s),
        "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY": str(settings.app_server_reconnect_initial_delay_s),
        "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY": str(settings.app_server_reconnect_max_delay_s),
        "IMCODEX_APP_SERVER_RECONNECT_JITTER": str(settings.app_server_reconnect_jitter_fraction),
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
        "IMCODEX_WEIXIN_ENABLED": "1" if settings.weixin_enabled else "0",
        "IMCODEX_WEIXIN_STATE_DIR": str(settings.weixin_state_dir or settings.data_dir / "channels" / "weixin"),
        "IMCODEX_WEIXIN_POLL_TIMEOUT_MS": str(settings.weixin_poll_timeout_ms),
    }
    dotenv_imported_keys = _environment_key_list(DOTENV_IMPORTED_KEYS_ENV)
    launcher_reloadable_keys = _environment_key_list(LAUNCHER_RELOADABLE_KEYS_ENV)
    reloadable_source_keys = set(dotenv_imported_keys) | set(launcher_reloadable_keys)
    external_setting_keys = _external_setting_keys(reloadable_source_keys=reloadable_source_keys)
    observability.write_launch_snapshot(
        command=[sys.executable, "-m", "imcodex"],
        cwd=Path.cwd(),
        # Restart reconstruction uses source metadata below. Persisting the
        # resolved values would duplicate configuration and could expose
        # environment-only credentials or identifiers.
        env={},
        settings_source=resolved_settings_source,
        restart_supported=resolved_settings_source == "environment",
        reload_env_keys=_reloadable_snapshot_keys(
            launch_env,
            reloadable_source_keys=reloadable_source_keys,
            external_setting_keys=external_setting_keys,
        ),
        dotenv_imported_keys=dotenv_imported_keys,
        launcher_reloadable_keys=launcher_reloadable_keys,
        required_external_env_keys=sorted(external_setting_keys),
    )
    return AppRuntime(
        client=client,
        service=service,
        managed_channels=managed_channels,
        observability=observability,
    )


def preflight_runtime_configuration() -> None:
    """Validate reconstructed startup inputs without opening native or IM transports."""

    try:
        settings = Settings.from_env()
    except Exception as exc:
        raise RuntimeError(
            f"settings could not be parsed ({type(exc).__name__})"
        ) from exc
    _validate_http_bind(settings)
    _validate_local_app_server_prerequisites(settings)
    runtime = build_runtime(settings, settings_source="environment")
    for channel in runtime.managed_channels:
        validator = getattr(channel, "validate_startup_configuration", None)
        if callable(validator):
            validator()


def run_runtime_preflight() -> int:
    """CLI-safe preflight wrapper that emits one bounded, non-secret diagnosis."""

    try:
        preflight_runtime_configuration()
    except Exception as exc:
        detail = " ".join(str(exc).split())[:400] or type(exc).__name__
        sys.stderr.write(f"{type(exc).__name__}: {detail}\n")
        return 1
    return 0


def _validate_http_bind(settings: Settings) -> None:
    if not 1 <= settings.http_port <= 65535:
        raise ValueError("IMCODEX_HTTP_PORT must be an integer between 1 and 65535")
    try:
        addresses = socket.getaddrinfo(
            settings.http_host,
            settings.http_port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror as exc:
        raise ValueError("IMCODEX_HTTP_HOST could not be resolved for the bridge listener") from exc
    if not addresses:
        raise ValueError("IMCODEX_HTTP_HOST did not resolve to a usable bridge listener address")
    if not _addresses_can_bind(addresses, port=0):
        raise ValueError("IMCODEX_HTTP_HOST does not identify a local bindable address")

    current_host = os.environ.get(PREFLIGHT_CURRENT_HTTP_HOST_ENV, "").strip()
    current_port = _preflight_current_port()
    must_test_port = current_port is not None and (
        current_port != settings.http_port
        or not _binds_may_overlap(current_host, settings.http_host)
    )
    if must_test_port and not _addresses_can_bind(addresses, port=settings.http_port):
        raise ValueError(
            "IMCODEX_HTTP_HOST and IMCODEX_HTTP_PORT are not available for the replacement bridge"
        )


def _preflight_current_port() -> int | None:
    raw = os.environ.get(PREFLIGHT_CURRENT_HTTP_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("Restart preflight received an invalid current HTTP port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Restart preflight received an invalid current HTTP port")
    return port


def _addresses_can_bind(addresses: list[tuple], *, port: int) -> bool:
    probes: list[socket.socket] = []
    try:
        for family, socktype, protocol, _canonical_name, sockaddr in set(addresses):
            try:
                probe = socket.socket(family, socktype, protocol)
            except OSError:
                continue
            probes.append(probe)
            if os.name == "posix" and sys.platform != "cygwin":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, True)
            target = list(sockaddr)
            target[1] = port
            probe.bind(tuple(target))
    except OSError:
        return False
    finally:
        for probe in probes:
            probe.close()
    return bool(probes)


def _binds_may_overlap(current_host: str, replacement_host: str) -> bool:
    if not current_host:
        return True
    current = _resolved_host_addresses(current_host)
    replacement = _resolved_host_addresses(replacement_host)
    if not current or not replacement:
        return True
    if any(address.is_unspecified for address in current | replacement):
        return True
    return bool(current & replacement)


def _resolved_host_addresses(host: str) -> set[object]:
    addresses: set[object] = set()
    try:
        results = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return addresses
    for _family, _socktype, _protocol, _canonical_name, sockaddr in results:
        try:
            addresses.add(ipaddress.ip_address(sockaddr[0].split("%", 1)[0]))
        except ValueError:
            continue
    return addresses


def _validate_local_app_server_prerequisites(settings: Settings) -> None:
    target = settings.app_server_target
    if target.transport == "stdio-jsonl" and shutil.which(settings.codex_bin) is None:
        raise RuntimeError(f"Codex executable was not found: {settings.codex_bin}")
    if target.transport == "unix-websocket":
        if os.name == "nt":
            raise RuntimeError(
                "unix app-server endpoints are not supported on native Windows; use ws:// or wss://"
            )
        resolve_unix_socket_path(target.endpoint)
    if settings.app_server_auth_token or settings.app_server_auth_token_file is None:
        return
    path = settings.app_server_auth_token_file
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read app-server auth token file: {path}") from exc
    if not token:
        raise ValueError(f"app-server auth token file is empty: {path}")


def _environment_key_list(marker: str) -> list[str]:
    return sorted({key.strip() for key in os.environ.get(marker, "").split(",") if key.strip()})


def _external_setting_keys(*, reloadable_source_keys: set[str]) -> set[str]:
    candidates = set(KNOWN_SETTING_ENV_KEYS) | {
        key for key in os.environ if is_restart_context_env_key(key)
    }
    return {
        key
        for key in candidates
        if key in os.environ
        and key not in reloadable_source_keys
        and (key not in TARGET_ENVIRONMENT_KEYS or bool(str(os.environ[key]).strip()))
    }


def _reloadable_snapshot_keys(
    launch_env: dict[str, str],
    *,
    reloadable_source_keys: set[str],
    external_setting_keys: set[str],
) -> list[str]:
    process_overrides = set(launch_env) & external_setting_keys
    if process_overrides & TARGET_ENVIRONMENT_KEYS:
        process_overrides.update(TARGET_ENVIRONMENT_KEYS)
    return sorted((set(launch_env) - process_overrides) | reloadable_source_keys)

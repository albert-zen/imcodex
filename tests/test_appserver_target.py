from __future__ import annotations

import pytest

from imcodex.appserver import (
    AppServerClient,
    AppServerSupervisor,
    AppServerTargetConfigError,
    parse_app_server_target,
    resolve_app_server_target,
)


@pytest.mark.parametrize(
    ("endpoint", "ownership", "transport", "connection_mode"),
    [
        ("unix://", "external", "unix-websocket", "external"),
        ("unix:///tmp/codex.sock", "external", "unix-websocket", "external"),
        ("unix:///tmp/codex@local.sock", "external", "unix-websocket", "external"),
        ("unix://run/codex@local.sock", "external", "unix-websocket", "external"),
        ("ws://127.0.0.1:8765", "external", "tcp-websocket", "external"),
        ("wss://codex.example.test/rpc", "external", "tcp-websocket", "external"),
        ("stdio://", "bridge-child", "stdio-jsonl", "spawned-stdio"),
    ],
)
def test_parse_app_server_target_reports_ownership_and_transport(
    endpoint: str,
    ownership: str,
    transport: str,
    connection_mode: str,
) -> None:
    target = parse_app_server_target(endpoint)

    assert target.endpoint == endpoint
    assert target.ownership == ownership
    assert target.transport == transport
    assert target.connection_mode == connection_mode
    assert target.preserves_server_state is (ownership == "external")


def test_resolve_app_server_target_defaults_to_the_native_unix_control_socket() -> None:
    target = resolve_app_server_target(os_name="posix")

    assert target.endpoint == "unix://"
    assert target.is_external is True


def test_resolve_app_server_target_defaults_to_an_external_tcp_server_on_windows() -> None:
    target = resolve_app_server_target(os_name="nt")

    assert target.endpoint == "ws://127.0.0.1:8765"
    assert target.is_external is True


@pytest.mark.parametrize(
    ("endpoint", "verified_shared_filesystem", "supported"),
    [
        ("stdio://", False, True),
        ("unix://", False, True),
        ("ws://localhost:8765", False, False),
        ("ws://127.0.0.1:8765", False, False),
        ("ws://[::1]:8765", False, False),
        ("wss://codex.example.test/rpc", False, False),
        ("ws://127.0.0.1:8765", True, True),
    ],
)
@pytest.mark.asyncio
async def test_app_server_client_only_exposes_shared_local_image_paths(
    endpoint: str,
    verified_shared_filesystem: bool,
    supported: bool,
) -> None:
    client = AppServerClient(
        supervisor=AppServerSupervisor(app_server_url=endpoint),
        client_info={"name": "test", "title": "Test", "version": "0"},
        shared_filesystem_verifier=lambda: verified_shared_filesystem,
    )
    await client._refresh_verified_shared_filesystem()

    assert client.supports_local_image_paths() is supported
    assert client.connection_facts()["local_image_paths"] is supported


def test_app_server_client_keeps_legacy_text_turn_input_compatibility() -> None:
    assert AppServerClient._resolve_turn_input(text="hello", input_items=None) == [
        {"type": "text", "text": "hello"}
    ]


@pytest.mark.parametrize("legacy_mode", ["dedicated-ws", "shared-ws"])
def test_legacy_websocket_modes_are_external_aliases(legacy_mode: str) -> None:
    explicit = resolve_app_server_target(
        app_server_url="unix:///tmp/codex.sock",
        core_mode=legacy_mode,
    )
    implicit = resolve_app_server_target(core_mode=legacy_mode)

    assert explicit.endpoint == "unix:///tmp/codex.sock"
    assert explicit.connection_mode == "external"
    assert implicit.endpoint == "ws://127.0.0.1:8765"
    assert implicit.connection_mode == "external"


@pytest.mark.parametrize("legacy_mode", ["stdio", "spawned-stdio"])
def test_legacy_stdio_modes_select_the_explicit_compatibility_target(legacy_mode: str) -> None:
    target = resolve_app_server_target(core_mode=legacy_mode)

    assert target.endpoint == "stdio://"
    assert target.ownership == "bridge-child"


def test_auto_mode_is_rejected_instead_of_falling_back_to_a_different_server() -> None:
    with pytest.raises(AppServerTargetConfigError, match="silently changes App Server lifecycle"):
        resolve_app_server_target(
            app_server_url="ws://127.0.0.1:8765",
            core_mode="auto",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "app_server_url": "ws://127.0.0.1:8765",
            "core_url": "ws://127.0.0.1:9001",
        },
        {"app_server_url": "stdio://", "core_mode": "shared-ws"},
        {"app_server_url": "unix://", "core_mode": "spawned-stdio"},
        {"app_server_url": "http://127.0.0.1:8765"},
        {"app_server_url": "wss://user:secret@example.test/rpc"},
        {"app_server_url": "wss://example.test/rpc?token=secret"},
        {"app_server_url": "wss://example.test/rpc#debug"},
        {"app_server_url": "unix://user:secret@run/codex.sock"},
        {"app_server_url": "unix:///tmp/codex.sock?token=secret"},
        {"app_server_url": "unix:///tmp/codex.sock#debug"},
        {"app_server_url": "UNIX:///tmp/codex.sock"},
        {"core_mode": "mystery"},
    ],
)
def test_invalid_or_conflicting_targets_fail_explicitly(payload: dict[str, str]) -> None:
    with pytest.raises(AppServerTargetConfigError):
        resolve_app_server_target(**payload)

from __future__ import annotations

import os
from pathlib import Path

import pytest

from imcodex.composition import _managed_core_shared_filesystem_verifier, build_runtime
from imcodex.config import Settings
from imcodex.sdk_runtime import SdkRuntime


@pytest.mark.asyncio
async def test_managed_core_verifier_rechecks_current_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    endpoint = "ws://127.0.0.1:8765"
    verified_urls = iter([endpoint, "ws://127.0.0.1:9999"])
    calls: list[object] = []

    class Manager:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def verify(self, *, port: int):
            calls.append(port)
            return type("Manifest", (), {"url": next(verified_urls)})()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("imcodex.composition.DedicatedCoreManager", Manager)
    settings = type(
        "SettingsLike",
        (),
        {"app_server_managed_target": endpoint, "codex_bin": "codex"},
    )()
    verifier = _managed_core_shared_filesystem_verifier(
        settings=settings,
        endpoint=endpoint,
        os_name="nt",
    )

    assert verifier is not None
    assert await verifier() is True
    assert await verifier() is False
    assert calls[1:] == [8765, 8765]


@pytest.mark.parametrize(
    "endpoint",
    (
        "ws://localhost:8765",
        "wss://127.0.0.1:8765",
        "wss://core.example.test/rpc",
    ),
)
def test_managed_core_verifier_rejects_noncanonical_targets(endpoint: str) -> None:
    settings = type(
        "SettingsLike",
        (),
        {"app_server_managed_target": endpoint, "codex_bin": "codex"},
    )()

    assert (
        _managed_core_shared_filesystem_verifier(
            settings=settings,
            endpoint=endpoint,
            os_name="nt",
        )
        is None
    )


def test_managed_core_verifier_rejects_non_windows() -> None:
    endpoint = "ws://127.0.0.1:8765"
    settings = type(
        "SettingsLike",
        (),
        {"app_server_managed_target": endpoint, "codex_bin": "codex"},
    )()

    assert (
        _managed_core_shared_filesystem_verifier(
            settings=settings,
            endpoint=endpoint,
            os_name="posix",
        )
        is None
    )


@pytest.mark.asyncio
async def test_build_runtime_constructs_only_sdk_runtime(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".imcodex",
        run_dir=tmp_path / ".imcodex-run",
        codex_bin="codex",
        app_server_url=None,
        app_server_experimental_api_enabled=False,
        core_mode="dedicated-ws",
        core_url="ws://127.0.0.1:8765",
        restart_executor=None,
        debug_api_enabled=False,
        log_level="INFO",
        http_host="127.0.0.1",
        http_port=8000,
        outbound_url=None,
        service_name="imcodex",
        qq_enabled=False,
        qq_app_id="",
        qq_client_secret="",
        qq_api_base="https://api.sgroup.qq.com",
        qq_markdown_enabled=False,
        native_thread_tool_host=False,
        app_server_managed_target="ws://127.0.0.1:8765",
        app_server_reconnect_initial_delay_s=0.6,
        app_server_reconnect_max_delay_s=45.0,
        app_server_reconnect_jitter_fraction=0.15,
    )

    runtime = build_runtime(settings)
    try:
        assert isinstance(runtime, SdkRuntime)
        assert runtime.observability.run_root == settings.run_dir
        assert runtime.client._supervisor.connection_target == "ws://127.0.0.1:8765"
        assert runtime.client._experimental_api_enabled is False
        assert runtime.client.supports_local_image_paths() is False
        assert (runtime.client._shared_filesystem_verifier is not None) is (
            os.name == "nt"
        )
        assert runtime.client._reconnect_retry_policy.initial_delay_s == 0.6
        assert runtime.client._reconnect_retry_policy.max_delay_s == 45.0
        assert runtime.client._reconnect_retry_policy.jitter_fraction == 0.15
    finally:
        await runtime.state.close()

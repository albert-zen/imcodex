from __future__ import annotations

from pathlib import Path

import pytest
from imagent.applications.appserver_client import AppServerClient
from imagent.storage import SQLiteGatewayState

from imcodex.channels.sdk_webhook import SdkWebhookChannel
from imcodex.config import Settings
from imcodex.sdk_composition import build_sdk_composition


def _settings(tmp_path: Path, **changes) -> Settings:
    values = dict(
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        codex_bin="codex",
        app_server_url="stdio://",
        app_server_experimental_api_enabled=False,
        core_mode=None,
        core_url=None,
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
        qq_markdown_enabled=True,
    )
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_sdk_composition_uses_one_gateway_state_and_sdk_client(tmp_path: Path) -> None:
    composition = build_sdk_composition(_settings(tmp_path))
    try:
        assert isinstance(composition.client, AppServerClient)
        assert isinstance(composition.state, SQLiteGatewayState)
        assert len(composition.channels) == 1
        assert isinstance(composition.channels[0], SdkWebhookChannel)
        assert composition.application.summary.ref.application_instance_id == "codex-main"
        assert composition.gateway._bindings is composition.state
        assert composition.gateway._idempotency is composition.state
        assert composition.gateway._projection_runtime._projections is composition.state
        assert composition.controller.service.backend.client is composition.client
    finally:
        await composition.controller.close()
        await composition.state.close()


@pytest.mark.asyncio
async def test_sdk_composition_builds_sdk_native_channel_from_product_config(
    tmp_path: Path,
) -> None:
    composition = build_sdk_composition(
        _settings(
            tmp_path,
            telegram_enabled=True,
            telegram_bot_token="token",
            telegram_require_mention=False,
        )
    )
    try:
        assert len(composition.channels) == 2
        assert isinstance(composition.channels[0], SdkWebhookChannel)
        channel = composition.channels[1]
        assert channel.channel_instance_id == "telegram"
        assert channel.kind == "telegram"
    finally:
        await composition.controller.close()
        await composition.state.close()

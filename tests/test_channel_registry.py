from __future__ import annotations

from types import SimpleNamespace

import pytest

from imcodex.channels import (
    FeishuChannelAdapter,
    QQChannelAdapter,
    TelegramChannelAdapter,
    WeixinChannelAdapter,
)
from imcodex.channels.registry import build_enabled_channel_adapters, get_channel_adapter_registry


def test_channel_registry_contains_qq_adapter() -> None:
    registry = get_channel_adapter_registry()

    assert registry["qq"] is QQChannelAdapter
    assert registry["telegram"] is TelegramChannelAdapter
    assert registry["feishu"] is FeishuChannelAdapter
    assert registry["weixin"] is WeixinChannelAdapter


def test_build_enabled_channel_adapters_uses_settings_channel_configs() -> None:
    middleware = object()
    settings = SimpleNamespace(
        channel_configs=lambda: {
            "qq": {
                "enabled": True,
                "app_id": "app",
                "client_secret": "secret",
                "api_base": "https://api.sgroup.qq.com",
                "markdown_enabled": True,
            },
        }
    )

    adapters = build_enabled_channel_adapters(settings=settings, middleware=middleware)

    assert len(adapters) == 1
    assert isinstance(adapters[0], QQChannelAdapter)
    assert adapters[0].middleware is middleware
    assert adapters[0].markdown_enabled is True


def test_build_enabled_channel_adapters_rejects_unknown_enabled_channel() -> None:
    settings = SimpleNamespace(
        channel_configs=lambda: {
            "unknown": {"enabled": True},
        }
    )

    with pytest.raises(RuntimeError, match="Unsupported enabled channel: unknown"):
        build_enabled_channel_adapters(settings=settings, middleware=object())

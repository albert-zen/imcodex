from __future__ import annotations

from .base import BaseChannelAdapter
from .feishu import FeishuChannelAdapter
from .qq import QQChannelAdapter
from .telegram import TelegramChannelAdapter
from .weixin import WeixinChannelAdapter

BUILTIN_CHANNEL_IDS = frozenset({"qq", "telegram", "feishu", "weixin"})


def get_channel_adapter_registry() -> dict[str, type[BaseChannelAdapter]]:
    return {
        "qq": QQChannelAdapter,
        "telegram": TelegramChannelAdapter,
        "feishu": FeishuChannelAdapter,
        "weixin": WeixinChannelAdapter,
    }


def build_enabled_channel_adapters(*, settings, middleware) -> list[object]:
    adapters: list[object] = []
    registry = get_channel_adapter_registry()
    for channel_id, config in settings.channel_configs().items():
        if not bool(config.get("enabled")):
            continue
        adapter_cls = registry.get(channel_id)
        if adapter_cls is None:
            raise RuntimeError(f"Unsupported enabled channel: {channel_id}")
        adapters.append(adapter_cls.from_config(config=config, middleware=middleware))
    return adapters

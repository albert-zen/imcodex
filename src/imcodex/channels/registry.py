from __future__ import annotations

from .qq import QQChannelAdapter


def get_channel_adapter_registry() -> dict[str, type[QQChannelAdapter]]:
    return {"qq": QQChannelAdapter}


def build_enabled_channel_adapters(*, settings, middleware) -> list[object]:
    adapters: list[object] = []
    registry = get_channel_adapter_registry()
    for channel_id, config in settings.channel_configs().items():
        if not bool(config.get("enabled")):
            continue
        adapter_cls = registry.get(channel_id)
        if adapter_cls is None:
            continue
        adapters.append(adapter_cls.from_config(config=config, middleware=middleware))
    return adapters

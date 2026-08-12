from __future__ import annotations

from imagent.channels import channel_from_config

BUILTIN_CHANNEL_IDS = frozenset({"qq", "telegram", "feishu", "weixin"})


def get_channel_adapter_registry() -> dict[str, object]:
    """Return the public SDK factory for each product-configured Channel."""

    return {channel_id: channel_from_config for channel_id in BUILTIN_CHANNEL_IDS}


def build_enabled_channel_adapters(*, settings, middleware=None) -> list[object]:
    del middleware
    adapters: list[object] = []
    for channel_id, config in settings.channel_configs().items():
        if not bool(config.get("enabled")):
            continue
        if channel_id not in BUILTIN_CHANNEL_IDS:
            raise RuntimeError(f"Unsupported enabled channel: {channel_id}")
        adapters.append(
            channel_from_config(
                channel_id,
                config=dict(config),
                channel_instance_id=channel_id,
            )
        )
    return adapters

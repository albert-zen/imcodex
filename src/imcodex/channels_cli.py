from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
from pathlib import Path
from typing import Callable

from .channels.access import ChannelAccessPolicy
from .channels.feishu import FeishuChannelAdapter
from .channels.telegram import read_telegram_bot_token_file
from .channels.weixin_ilink import ILinkError, WeixinILinkTransport
from .channels.weixin_login import WeixinLoginError, WeixinLoginFlow
from .channels.weixin_state import WeixinStateStore
from .config import Settings


def build_channels_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m imcodex channels")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="List built-in channel adapters and enabled state.")
    subparsers.add_parser(
        "doctor",
        help="Validate enabled channel configuration without revealing secrets.",
    )

    login = subparsers.add_parser("login", help="Run an interactive channel login.")
    login.add_argument("channel", choices=["weixin"])
    login.add_argument("--timeout", type=float, default=480.0)

    logout = subparsers.add_parser("logout", help="Remove local channel credentials and transport state.")
    logout.add_argument("channel", choices=["weixin"])
    logout.add_argument("--yes", action="store_true", help="Do not prompt for confirmation.")
    return parser


def run_channels_cli(
    argv: list[str],
    *,
    settings: Settings | None = None,
    transport_factory: Callable[[], object] | None = None,
    output: Callable[[str], object] = print,
    input_func: Callable[[str], str] = input,
) -> int:
    args = build_channels_parser().parse_args(argv)
    settings = settings or Settings.from_env()
    command = args.command or "list"
    if command == "list":
        return _list_channels(settings, output=output)
    if command == "doctor":
        return _doctor(settings, output=output)
    if command == "login":
        state_store = WeixinStateStore(_weixin_state_dir(settings))
        transport = transport_factory() if transport_factory is not None else WeixinILinkTransport()

        async def login() -> None:
            try:
                flow = WeixinLoginFlow(
                    state_store=state_store,
                    transport=transport,
                    input_func=input_func,
                    output=output,
                )
                credentials = await flow.login(timeout_s=args.timeout)
                output(f"Account: {credentials.account_id}")
                output(f"Owner: {credentials.owner_user_id or '(not reported by platform)'}")
                output(f"State: {state_store.root}")
                output("Restart the bridge to load the new Weixin credentials.")
            finally:
                with contextlib.suppress(Exception):
                    await transport.close()

        try:
            asyncio.run(login())
        except (ILinkError, WeixinLoginError, RuntimeError, ValueError) as exc:
            output(f"Weixin login failed: {exc}")
            return 1
        return 0
    if command == "logout":
        if not args.yes:
            answer = input_func("Remove local Weixin credentials and transport state? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                output("Cancelled.")
                return 1
        WeixinStateStore(_weixin_state_dir(settings)).clear()
        output(
            "Local Weixin credentials and transport state removed. "
            "Restart any running bridge to discard in-memory credentials."
        )
        return 0
    return 2


def _list_channels(settings: Settings, *, output: Callable[[str], object]) -> int:
    output("Built-in channels:")
    output(f"  qq        {'enabled' if settings.qq_enabled else 'disabled'}")
    output(f"  telegram  {'enabled' if settings.telegram_enabled else 'disabled'}")
    output(f"  feishu    {'enabled' if settings.feishu_enabled else 'disabled'} ({settings.feishu_domain})")
    output(f"  weixin    {'enabled' if settings.weixin_enabled else 'disabled'} (experimental)")
    return 0


def _doctor(settings: Settings, *, output: Callable[[str], object]) -> int:
    failures: list[str] = []
    for channel_id, config in settings.channel_configs().items():
        if not bool(config.get("enabled")):
            continue
        try:
            ChannelAccessPolicy.from_config(config)
        except ValueError as exc:
            failures.append(f"{channel_id}: invalid access restrictions ({exc})")
    if settings.qq_enabled:
        if not settings.qq_app_id or not settings.qq_client_secret:
            failures.append("qq: missing App ID or Client Secret")
    if settings.telegram_enabled:
        token_file_ok = False
        token_file_invalid = False
        if not settings.telegram_bot_token.strip() and settings.telegram_bot_token_file:
            try:
                token_file_ok = bool(read_telegram_bot_token_file(settings.telegram_bot_token_file))
            except RuntimeError as exc:
                token_file_invalid = True
                failures.append(f"telegram: {exc}")
        if not settings.telegram_bot_token.strip() and not token_file_ok and not token_file_invalid:
            failures.append("telegram: missing bot token or readable token file")
    if settings.feishu_enabled:
        if not settings.feishu_app_id or not settings.feishu_app_secret:
            failures.append("feishu: missing App ID or App Secret")
        try:
            FeishuChannelAdapter._normalize_domain(settings.feishu_domain)
        except ValueError as exc:
            failures.append(f"feishu: invalid domain ({exc})")
        if importlib.util.find_spec("lark_channel") is None:
            failures.append("feishu: optional dependency missing; install .[feishu]")
    if settings.weixin_enabled:
        state_store = WeixinStateStore(_weixin_state_dir(settings))
        try:
            credentials = state_store.load_credentials()
            state_store.load_transport_state()
        except RuntimeError as exc:
            failures.append(f"weixin: {exc}")
        else:
            if credentials is None:
                failures.append("weixin: not logged in; run channels login weixin")
    if failures:
        output("Channel doctor found configuration problems:")
        for failure in failures:
            output(f"  - {failure}")
        return 1
    output("Channel configuration looks ready.")
    return 0


def _weixin_state_dir(settings: Settings) -> Path:
    return settings.weixin_state_dir or settings.data_dir / "channels" / "weixin"

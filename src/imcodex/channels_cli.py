from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import mimetypes
from pathlib import Path
from typing import Callable
import uuid

import httpx

from .channels.access import ChannelAccessPolicy
from .channels.feishu import FeishuChannelAdapter
from .channels.telegram import read_telegram_bot_token_file
from .channels.weixin_ilink import ILinkError, WeixinILinkTransport
from .channels.weixin_login import WeixinLoginError, WeixinLoginFlow
from .channels.weixin_state import WeixinStateStore
from .config import Settings
from .delivery_api import (
    DELIVERY_PATH,
    DELIVERY_TOKEN_FILE,
    DELIVERY_TOKEN_HEADER,
    MAX_DELIVERY_ARTIFACTS,
)
from .observability.health import BRIDGE_INSTANCE_HEADER


def build_channels_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m imcodex channels")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="List built-in channel adapters and enabled state.")
    subparsers.add_parser(
        "doctor",
        help="Validate enabled channel configuration without revealing secrets.",
    )

    send = subparsers.add_parser(
        "send",
        help="Send text and artifacts through the running local bridge.",
    )
    send.add_argument("--channel", required=True)
    send.add_argument("--conversation", required=True)
    send.add_argument("--text", default="")
    send.add_argument("--artifact", action="append", default=[])
    send.add_argument("--delivery-id", default="")

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
    if command == "send":
        return _send(
            settings,
            channel_id=args.channel,
            conversation_id=args.conversation,
            text_value=args.text,
            artifact_values=args.artifact,
            delivery_id=args.delivery_id,
            output=output,
        )
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


def _send(
    settings: Settings,
    *,
    channel_id: str,
    conversation_id: str,
    text_value: str,
    artifact_values: list[str],
    delivery_id: str,
    output: Callable[[str], object],
) -> int:
    if len(artifact_values) > MAX_DELIVERY_ARTIFACTS:
        output(json.dumps({"status": "invalid", "error": "At most 4 artifacts are supported."}))
        return 2
    root = Path.cwd().resolve()
    uploads = []
    manifest = []
    try:
        for value in artifact_values:
            candidate = Path(value).expanduser()
            if candidate.is_symlink():
                raise ValueError(f"Artifact path must not be a symlink: {candidate.name}")
            path = candidate.resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Artifact is not a regular workspace file: {path.name}")
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            kind = "image" if content_type.startswith("image/") else "file"
            uploads.append(("artifacts", (path.name, content, content_type)))
            manifest.append(
                {"kind": kind, "filename": path.name, "content_type": content_type}
            )
    except (OSError, ValueError) as exc:
        output(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    if not text_value.strip() and not uploads:
        output(json.dumps({"status": "invalid", "error": "Delivery has no content."}))
        return 2
    try:
        target, instance_id, delivery_token = _running_bridge_target(settings)
        payload = {
            "channel_id": channel_id,
            "conversation_id": conversation_id,
            "text": text_value,
            "delivery_id": delivery_id.strip() or f"imcodex-tool:{uuid.uuid4().hex}",
            "artifacts": manifest,
        }
        response = httpx.post(
            f"{target}{DELIVERY_PATH}",
            headers={
                BRIDGE_INSTANCE_HEADER: instance_id,
                DELIVERY_TOKEN_HEADER: delivery_token,
            },
            data={"payload": json.dumps(payload, ensure_ascii=False)},
            files=uploads or None,
            timeout=60.0,
        )
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("bridge returned a non-object receipt")
    except (OSError, ValueError, httpx.HTTPError) as exc:
        output(
            json.dumps(
                {"status": "failed", "error": f"Local bridge delivery failed: {type(exc).__name__}"},
                ensure_ascii=False,
            )
        )
        return 1
    output(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if response.is_success and result.get("status") == "delivered":
        return 0
    if result.get("status") == "partial":
        return 3
    return 1


def _running_bridge_target(settings: Settings) -> tuple[str, str, str]:
    health_path = settings.run_dir / "current" / "health.json"
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
        http = health["http"]
        host = str(http["host"])
        port = int(http["port"])
        instance_id = str(health["instance_id"])
        delivery_token = (
            settings.run_dir / "current" / DELIVERY_TOKEN_FILE
        ).read_text(encoding="utf-8").strip()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("running bridge health metadata is unavailable") from None
    if not bool(http.get("listening")) or not instance_id or not delivery_token:
        raise ValueError("bridge is not listening")
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}", instance_id, delivery_token

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from imcodex.channels.weixin_ilink import ILinkError, WeixinILinkTransport
from imcodex.channels.weixin_state import (
    WeixinCredentials,
    WeixinStateStore,
    WeixinTransportState,
)


def test_weixin_state_store_round_trips_sensitive_state_with_private_permissions(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    credentials = WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        owner_user_id="owner@im.wechat",
    )
    state = WeixinTransportState(get_updates_buf="cursor")
    state.set_context_token("owner@im.wechat", "context-secret")

    store.save_credentials(credentials)
    store.save_transport_state(state)

    assert store.load_credentials() == WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        owner_user_id="owner@im.wechat",
        saved_at=store.load_credentials().saved_at,
    )
    assert store.load_transport_state() == state
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.credentials_path.stat().st_mode & 0o777 == 0o600
    assert store.transport_state_path.stat().st_mode & 0o777 == 0o600


def test_weixin_transport_state_bounds_context_tokens() -> None:
    state = WeixinTransportState()

    for index in range(4):
        state.set_context_token(f"u{index}", f"token{index}", limit=3)

    assert state.context_tokens == {"u1": "token1", "u2": "token2", "u3": "token3"}


def test_weixin_state_store_recovers_from_corrupt_transport_but_not_credentials(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path)
    store.transport_state_path.write_text("not-json", encoding="utf-8")
    store.credentials_path.write_text("not-json", encoding="utf-8")

    assert store.load_transport_state() == WeixinTransportState()
    with pytest.raises(RuntimeError, match="Could not read Weixin state file"):
        store.load_credentials()


@pytest.mark.asyncio
async def test_ilink_fetch_qr_uses_required_compatibility_headers() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"qrcode": "qr-secret", "qrcode_img_content": "https://example/qr"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        result = await transport.fetch_qr_code(local_tokens=["old-token"])

    assert result["qrcode"] == "qr-secret"
    request = requests[0]
    assert request.url.path == "/ilink/bot/get_bot_qrcode"
    assert request.url.params["bot_type"] == "3"
    assert request.headers["ilink-app-id"] == "bot"
    assert request.headers["ilink-app-clientversion"] == "132102"
    assert request.headers["authorizationtype"] == "ilink_bot_token"
    assert base64.b64decode(request.headers["x-wechat-uin"]).decode("ascii").isdigit()
    assert json.loads(request.content) == {"local_token_list": ["old-token"]}


@pytest.mark.asyncio
async def test_ilink_get_updates_sends_cursor_and_bearer_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": "next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(
            token="bot-secret",
            http_client=client,
        )
        result = await transport.get_updates(get_updates_buf="cursor", timeout_ms=35_000)

    assert result["get_updates_buf"] == "next"
    request = requests[0]
    assert request.headers["authorization"] == "Bearer bot-secret"
    body = json.loads(request.content)
    assert body["get_updates_buf"] == "cursor"
    assert body["base_info"]["bot_agent"] == "IMCodex/0.1.0"


@pytest.mark.asyncio
async def test_ilink_send_text_retries_rate_limit_with_stable_client_id() -> None:
    attempts: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ret": 0})

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(
            token="bot-secret",
            http_client=client,
            sleep=capture_sleep,
        )
        client_id = await transport.send_text(
            to_user_id="owner@im.wechat",
            text="done",
            context_token="context-secret",
            client_id="stable-id",
        )

    assert client_id == "stable-id"
    assert delays == [2.0]
    assert len(attempts) == 2
    bodies = [json.loads(request.content) for request in attempts]
    assert bodies[0] == bodies[1]
    assert bodies[0]["msg"] == {
        "from_user_id": "",
        "to_user_id": "owner@im.wechat",
        "client_id": "stable-id",
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": "done"}}],
        "context_token": "context-secret",
    }


@pytest.mark.asyncio
async def test_ilink_send_text_rejects_nonzero_protocol_result() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": -14, "errmsg": "stale"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(token="bot-secret", http_client=client)
        with pytest.raises(ILinkError) as exc_info:
            await transport.send_text(
                to_user_id="owner@im.wechat",
                text="done",
                context_token="context-secret",
            )

    assert exc_info.value.code == -14
    assert "bot-secret" not in str(exc_info.value)
    assert "context-secret" not in str(exc_info.value)


def test_ilink_rejects_insecure_base_url() -> None:
    with pytest.raises(ValueError, match="must be an HTTPS origin"):
        WeixinILinkTransport(base_url="http://ilinkai.weixin.qq.com")

    with pytest.raises(ValueError, match="official weixin.qq.com host"):
        WeixinILinkTransport(base_url="https://attacker.example")

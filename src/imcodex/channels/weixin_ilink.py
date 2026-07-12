from __future__ import annotations

import asyncio
import base64
import json
import secrets
from typing import Any
from urllib.parse import quote, urlparse
import uuid

import httpx

from .weixin_state import WeixinCredentials


DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
# Protocol compatibility version used by Tencent/openclaw-weixin 2.4.6.
ILINK_APP_CLIENT_VERSION = "132102"
BASE_INFO = {
    "channel_version": "0.1.0",
    "bot_agent": "IMCodex/0.1.0",
}


class ILinkError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class WeixinILinkTransport:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_ILINK_BASE_URL,
        token: str = "",
        http_client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.token = token.strip()
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
        self.sleep = sleep

    @classmethod
    def from_credentials(
        cls,
        credentials: WeixinCredentials,
        **kwargs,
    ) -> "WeixinILinkTransport":
        return cls(base_url=credentials.base_url, token=credentials.bot_token, **kwargs)

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def fetch_qr_code(self, *, local_tokens: list[str] | None = None) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "ilink/bot/get_bot_qrcode?bot_type=3",
            body={"local_token_list": list(local_tokens or [])[:10]},
            authenticated=False,
            max_attempts=3,
        )

    async def poll_qr_status(
        self,
        *,
        qrcode: str,
        verify_code: str = "",
        base_url: str | None = None,
    ) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        return await self._request_json(
            "GET",
            endpoint,
            authenticated=False,
            timeout_s=40.0,
            max_attempts=1,
            base_url=base_url,
        )

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "ilink/bot/getupdates",
            body={
                "get_updates_buf": get_updates_buf,
                "base_info": BASE_INFO,
            },
            timeout_s=max(5.0, timeout_ms / 1000.0 + 5.0),
            max_attempts=1,
        )

    async def send_text(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str,
        client_id: str | None = None,
    ) -> str:
        message_client_id = client_id or f"imcodex-weixin-{uuid.uuid4().hex}"
        payload = await self._request_json(
            "POST",
            "ilink/bot/sendmessage",
            body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": message_client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                    "context_token": context_token,
                },
                "base_info": BASE_INFO,
            },
            timeout_s=15.0,
            max_attempts=3,
        )
        self._raise_protocol_error(payload, operation="sendmessage")
        return message_client_id

    async def notify_start(self) -> dict[str, Any]:
        return await self._notify("ilink/bot/msg/notifystart")

    async def notify_stop(self) -> dict[str, Any]:
        return await self._notify("ilink/bot/msg/notifystop")

    async def _notify(self, endpoint: str) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            endpoint,
            body={"base_info": BASE_INFO},
            timeout_s=10.0,
            max_attempts=2,
        )
        self._raise_protocol_error(payload, operation=endpoint.rsplit("/", 1)[-1])
        return payload

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        body: dict[str, object] | None = None,
        authenticated: bool = True,
        timeout_s: float = 15.0,
        max_attempts: int,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        request_base = self._validate_base_url(base_url or self.base_url)
        url = f"{request_base.rstrip('/')}/{endpoint.lstrip('/')}"
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await self.http_client.request(
                    method,
                    url,
                    json=body if method != "GET" else None,
                    headers=self._headers(
                        authenticated=authenticated,
                        include_json_headers=method != "GET",
                    ),
                    timeout=timeout_s,
                )
            except httpx.HTTPError as exc:
                if attempt >= attempts:
                    raise ILinkError(f"iLink network request failed ({type(exc).__name__})") from None
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if response.status_code == 429 and attempt < attempts:
                await self.sleep(self._retry_after(response))
                continue
            if response.status_code >= 500 and attempt < attempts:
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if not response.is_success:
                raise ILinkError(
                    f"iLink request failed with HTTP {response.status_code}",
                    code=response.status_code,
                )
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                raise ILinkError("iLink returned an invalid JSON response") from None
            if not isinstance(payload, dict):
                raise ILinkError("iLink returned a non-object JSON response")
            return payload
        raise ILinkError("iLink request exhausted retry attempts")

    def _headers(self, *, authenticated: bool, include_json_headers: bool) -> dict[str, str]:
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
        }
        if authenticated or include_json_headers:
            random_uin = str(secrets.randbits(32)).encode("ascii")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(random_uin).decode("ascii"),
                }
            )
        if authenticated:
            if not self.token:
                raise ILinkError("iLink authenticated request requires a bot token")
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _raise_protocol_error(payload: dict[str, Any], *, operation: str) -> None:
        code_value = payload.get("errcode")
        if code_value in (None, 0):
            code_value = payload.get("ret")
        try:
            code = int(code_value or 0)
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise ILinkError(f"iLink {operation} failed with code {code}", code=code)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            return max(0.0, float(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1.0

    @staticmethod
    def _validate_base_url(value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("iLink base URL must be an HTTPS origin without credentials.")
        if parsed.query or parsed.fragment:
            raise ValueError("iLink base URL must not include a query or fragment.")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
            raise ValueError("iLink base URL must use an official weixin.qq.com host.")
        return value.strip().rstrip("/")

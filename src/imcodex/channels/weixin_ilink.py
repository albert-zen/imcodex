from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit
import uuid

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import httpx

from .media import MediaDownloadError
from .weixin_state import WeixinCredentials


DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_ILINK_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
# Protocol compatibility version used by Tencent/openclaw-weixin 2.4.6.
ILINK_APP_CLIENT_VERSION = "132102"
BASE_INFO = {
    "channel_version": "0.1.0",
    "bot_agent": "IMCodex/0.1.0",
}
_IMAGE_DOWNLOAD_TIMEOUT = httpx.Timeout(
    20.0,
    connect=5.0,
    read=15.0,
    write=5.0,
    pool=5.0,
)
_AES_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True, slots=True)
class WeixinImageReference:
    """Opaque iLink CDN reference whose secrets must never enter repr or logs."""

    encrypted_query_param: str = field(default="", repr=False)
    aes_key: str = field(default="", repr=False)
    full_url: str = field(default="", repr=False)
    aeskey: str = field(default="", repr=False)


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

    async def send_artifact(
        self,
        *,
        to_user_id: str,
        content: bytes,
        filename: str,
        kind: str,
        context_token: str,
        client_id: str | None = None,
    ) -> str:
        """Upload one staged image/file to Tencent CDN and send its iLink item."""

        if kind not in {"image", "file"}:
            raise ILinkError("unsupported iLink artifact kind", code=400)
        aes_key = secrets.token_bytes(16)
        ciphertext = await asyncio.to_thread(_encrypt_outbound_artifact, content, aes_key)
        file_key = secrets.token_hex(16)
        upload = await self._request_json(
            "POST",
            "ilink/bot/getuploadurl",
            body={
                "filekey": file_key,
                "media_type": 1 if kind == "image" else 3,
                "to_user_id": to_user_id,
                "rawsize": len(content),
                "rawfilemd5": hashlib.md5(content).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
                "base_info": BASE_INFO,
            },
            timeout_s=15.0,
            max_attempts=3,
        )
        self._raise_protocol_error(upload, operation="getuploadurl")
        upload_url = self._cdn_upload_url(
            upload_full_url=str(upload.get("upload_full_url") or ""),
            upload_param=str(upload.get("upload_param") or ""),
            file_key=file_key,
        )
        encrypted_param = await self._upload_cdn(upload_url, ciphertext)
        media = {
            "encrypt_query_param": encrypted_param,
            # Tencent's published plugin encodes the hexadecimal key string,
            # rather than the raw 16 bytes, for outbound CDNMedia.aes_key.
            "aes_key": base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        if kind == "image":
            item = {
                "type": 2,
                "image_item": {"media": media, "mid_size": len(ciphertext)},
            }
        else:
            item = {
                "type": 4,
                "file_item": {
                    "media": media,
                    "file_name": filename,
                    "len": str(len(content)),
                },
            }
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
                    "item_list": [item],
                    "context_token": context_token,
                },
                "base_info": BASE_INFO,
            },
            timeout_s=15.0,
            max_attempts=3,
        )
        self._raise_protocol_error(payload, operation="sendmessage")
        return message_client_id

    @staticmethod
    def _cdn_upload_url(
        *,
        upload_full_url: str,
        upload_param: str,
        file_key: str,
    ) -> str:
        if upload_full_url.strip():
            try:
                return _validate_weixin_media_url(upload_full_url)
            except MediaDownloadError:
                raise ILinkError("iLink getuploadurl returned an invalid upload target", code=400) from None
        if not upload_param.strip():
            raise ILinkError("iLink getuploadurl returned no upload target", code=400)
        return (
            f"{DEFAULT_ILINK_CDN_BASE_URL}/upload"
            f"?encrypted_query_param={quote(upload_param, safe='')}"
            f"&filekey={quote(file_key, safe='')}"
        )

    async def _upload_cdn(self, url: str, ciphertext: bytes) -> str:
        for attempt in range(1, 4):
            try:
                response = await self.http_client.put(
                    url,
                    content=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                    follow_redirects=False,
                    timeout=20.0,
                )
            except httpx.HTTPError as exc:
                if attempt >= 3:
                    raise ILinkError(
                        f"iLink CDN upload failed ({type(exc).__name__})"
                    ) from None
                await self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if 300 <= response.status_code < 400:
                raise ILinkError("iLink CDN upload refused a redirect", code=400)
            if 400 <= response.status_code < 500:
                raise ILinkError("iLink CDN rejected the upload", code=response.status_code)
            if response.status_code != 200:
                if attempt < 3:
                    await self.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise ILinkError("iLink CDN upload failed", code=response.status_code)
            encrypted_param = str(response.headers.get("x-encrypted-param") or "").strip()
            if not encrypted_param:
                raise ILinkError("iLink CDN upload returned no media reference", code=400)
            return encrypted_param
        raise ILinkError("iLink CDN upload exhausted retry attempts")

    async def download_image(
        self,
        reference: WeixinImageReference,
        write_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """Download one iLink image and stream decrypted plaintext to the media boundary."""

        url = _image_download_url(reference)
        key = _image_aes_key(reference)
        cipher = AES.new(key, AES.MODE_ECB) if key is not None else None
        pending = bytearray()

        try:
            async with self.http_client.stream(
                "GET",
                url,
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
                timeout=_IMAGE_DOWNLOAD_TIMEOUT,
            ) as response:
                if 300 <= response.status_code < 400 or not response.is_success:
                    raise MediaDownloadError
                content_encoding = (
                    response.headers.get("Content-Encoding", "").strip().casefold()
                )
                if content_encoding not in {"", "identity"}:
                    raise MediaDownloadError
                async for chunk in response.aiter_raw():
                    if not chunk:
                        continue
                    if cipher is None:
                        await write_chunk(chunk)
                        continue
                    pending.extend(chunk)
                    decrypt_size = max(
                        0,
                        ((len(pending) - AES.block_size) // AES.block_size)
                        * AES.block_size,
                    )
                    if decrypt_size:
                        # AES operates on a bounded network chunk in native
                        # code. Keep it owned by this task rather than leaving
                        # an unkillable executor job after cancellation.
                        plaintext = cipher.decrypt(bytes(pending[:decrypt_size]))
                        del pending[:decrypt_size]
                        await write_chunk(plaintext)
        except MediaDownloadError:
            raise
        except httpx.HTTPError:
            # httpx exceptions retain the signed CDN URL. Do not propagate it
            # into a later exception chain or diagnostic.
            raise MediaDownloadError from None

        if cipher is None:
            return
        if not pending or len(pending) % AES.block_size:
            raise MediaDownloadError
        try:
            final_block = cipher.decrypt(bytes(pending))
            final_plaintext = unpad(
                final_block,
                AES.block_size,
                style="pkcs7",
            )
        except ValueError:
            raise MediaDownloadError from None
        await write_chunk(final_plaintext)

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
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("iLink base URL must be an HTTPS origin without credentials.")
        if parsed.query or parsed.fragment:
            raise ValueError("iLink base URL must not include a query or fragment.")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
            raise ValueError("iLink base URL must use an official weixin.qq.com host.")
        return value.strip().rstrip("/")


def _image_download_url(reference: WeixinImageReference) -> str:
    full_url = reference.full_url.strip()
    if full_url:
        return _validate_weixin_media_url(full_url)
    query_param = reference.encrypted_query_param.strip()
    if not query_param or any(character.isspace() for character in query_param):
        raise MediaDownloadError
    return (
        f"{DEFAULT_ILINK_CDN_BASE_URL}/download"
        f"?encrypted_query_param={quote(query_param, safe='')}"
    )


def _encrypt_outbound_artifact(content: bytes, aes_key: bytes) -> bytes:
    return AES.new(aes_key, AES.MODE_ECB).encrypt(
        pad(content, AES.block_size, style="pkcs7")
    )


def _validate_weixin_media_url(value: str) -> str:
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise MediaDownloadError
    try:
        parsed = urlsplit(normalized)
        hostname = str(parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise MediaDownloadError from None
    if parsed.scheme.lower() != "https" or not parsed.netloc or not hostname:
        raise MediaDownloadError
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise MediaDownloadError
    if port not in (None, 443):
        raise MediaDownloadError
    if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
        raise MediaDownloadError
    return normalized


def _image_aes_key(reference: WeixinImageReference) -> bytes | None:
    preferred_hex = reference.aeskey.strip()
    if preferred_hex:
        if _AES_HEX_PATTERN.fullmatch(preferred_hex) is None:
            raise MediaDownloadError
        return bytes.fromhex(preferred_hex)

    encoded = reference.aes_key.strip()
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise MediaDownloadError from None
    if len(decoded) == AES.block_size:
        return decoded
    if len(decoded) == 32:
        try:
            hex_value = decoded.decode("ascii")
        except UnicodeDecodeError:
            raise MediaDownloadError from None
        if _AES_HEX_PATTERN.fullmatch(hex_value) is not None:
            return bytes.fromhex(hex_value)
    raise MediaDownloadError

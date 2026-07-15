from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .media import (
    IMAGE_DOWNLOAD_FAILED,
    IMAGE_TOO_LARGE,
    INVALID_IMAGE,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    MAX_IMAGE_PIXELS,
    MEDIA_MATERIALIZE_DEADLINE_S,
    MEDIA_QUOTA_BYTES,
    MEDIA_RETENTION_S,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE,
    ImageMediaMaterializer,
    MaterializedImage,
    MediaDownloadError,
    MediaResult,
)


_TRUSTED_MEDIA_HOSTS = frozenset(
    {
        "myqcloud.com",
        "qpic.cn",
        "qq.com",
        "qq.com.cn",
        "tencentcos.cn",
        "tencentcos.com",
        "ugcimg.cn",
        "weiyun.com",
    }
)
_DOWNLOAD_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class QQImageReference:
    url: str = field(repr=False)


# Backwards-compatible internal names while staging is now channel-neutral.
QQMaterializedImage = MaterializedImage
QQMediaResult = MediaResult


def parse_qq_image_references(value: object) -> tuple[QQImageReference, ...]:
    """Extract bounded attachment URLs without trusting platform metadata."""

    if not isinstance(value, list):
        return ()
    references: list[QQImageReference] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        references.append(QQImageReference(url=str(item.get("url") or "").strip()))
        # Preserve one item beyond the limit so the common materializer can
        # reject the whole message without retaining an unbounded list.
        if len(references) > MAX_IMAGE_COUNT:
            break
    return tuple(references)


class QQMediaMaterializer(ImageMediaMaterializer[QQImageReference]):
    def __init__(
        self,
        *,
        root: Path,
        http_client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
        cleanup_sleep=asyncio.sleep,
    ) -> None:
        self.http_client = http_client
        super().__init__(
            root=root,
            download=self._download_image,
            clock=clock,
            cleanup_sleep=cleanup_sleep,
        )

    async def _download_image(self, reference: QQImageReference, write_chunk) -> None:
        url = _normalize_media_url(reference.url)
        try:
            async with self.http_client.stream(
                "GET",
                url,
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
                timeout=_DOWNLOAD_TIMEOUT,
            ) as response:
                if 300 <= response.status_code < 400 or not response.is_success:
                    raise MediaDownloadError
                content_encoding = response.headers.get("Content-Encoding", "").strip().casefold()
                if content_encoding not in {"", "identity"}:
                    raise MediaDownloadError
                if response.is_stream_consumed:
                    # Mock/custom transports may hand httpx an already-loaded
                    # identity body. Real network responses stay on raw stream.
                    await write_chunk(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        await write_chunk(chunk)
        except MediaDownloadError:
            raise
        except httpx.HTTPError:
            # httpx exceptions retain the signed request URL. Keep that URL
            # out of later exception chains and diagnostics.
            raise MediaDownloadError from None


def _normalize_media_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized or any(character.isspace() for character in normalized):
        raise MediaDownloadError
    try:
        parsed = urlsplit(normalized)
        host = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise MediaDownloadError from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not host:
        raise MediaDownloadError
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise MediaDownloadError
    if port not in (None, 443):
        raise MediaDownloadError
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _TRUSTED_MEDIA_HOSTS):
        raise MediaDownloadError
    return normalized

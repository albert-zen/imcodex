from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit
import warnings

import httpx
from PIL import Image

from ..windows_security import secure_windows_path


MAX_IMAGE_COUNT = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MEDIA_RETENTION_S = 24 * 60 * 60
MEDIA_QUOTA_BYTES = 512 * 1024 * 1024
MEDIA_MATERIALIZE_DEADLINE_S = 30.0

IMAGE_TOO_LARGE = "image_too_large"
TOO_MANY_IMAGES = "too_many_images"
UNSUPPORTED_IMAGE = "unsupported_image"
INVALID_IMAGE = "invalid_image"
IMAGE_DOWNLOAD_FAILED = "image_download_failed"

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
    url: str


@dataclass(frozen=True, slots=True)
class QQMaterializedImage:
    content_type: str
    local_path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class QQMediaResult:
    images: tuple[QQMaterializedImage, ...] = ()
    input_error: str | None = None


class _ImageTooLargeError(Exception):
    pass


class _UnsupportedImageError(Exception):
    pass


class _InvalidImageError(Exception):
    pass


class _MediaDownloadError(Exception):
    pass


def parse_qq_image_references(value: object) -> tuple[QQImageReference, ...]:
    """Extract bounded attachment URLs without trusting platform metadata.

    QQ's declared MIME type and size are hints only. The materializer decides
    whether each attachment is an accepted image from the downloaded bytes and
    enforces the byte limit while streaming.
    """

    if not isinstance(value, list):
        return ()
    references: list[QQImageReference] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        references.append(QQImageReference(url=str(item.get("url") or "").strip()))
        # Preserve one item beyond the limit so the worker can reject the
        # complete message without retaining an attacker-controlled list.
        if len(references) > MAX_IMAGE_COUNT:
            break
    return tuple(references)


class QQMediaMaterializer:
    def __init__(
        self,
        *,
        root: Path,
        http_client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.http_client = http_client
        self.clock = clock
        # The quota bounds the whole spool. Serializing sweep + download keeps
        # concurrent callers from independently spending the same free bytes.
        self._spool_lock = asyncio.Lock()

    async def prepare(self) -> None:
        async with self._spool_lock:
            await asyncio.to_thread(self._prepare_and_sweep, False)

    async def materialize(self, references: tuple[QQImageReference, ...]) -> QQMediaResult:
        if not references:
            return QQMediaResult()
        if len(references) > MAX_IMAGE_COUNT:
            return QQMediaResult(input_error=TOO_MANY_IMAGES)

        async with self._spool_lock:
            try:
                async with asyncio.timeout(MEDIA_MATERIALIZE_DEADLINE_S):
                    return await self._materialize_locked(references)
            except TimeoutError:
                return QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)

    async def _materialize_locked(
        self,
        references: tuple[QQImageReference, ...],
    ) -> QQMediaResult:

        created: list[Path] = []
        materialized: list[QQMaterializedImage] = []
        try:
            usage = await asyncio.to_thread(self._prepare_and_sweep, True)
            for reference in references:
                remaining_quota = MEDIA_QUOTA_BYTES - usage
                if remaining_quota <= 0:
                    raise _MediaDownloadError
                image = await self._download_image(
                    reference,
                    remaining_quota=remaining_quota,
                )
                path = Path(image.local_path)
                created.append(path)
                materialized.append(image)
                usage += image.size_bytes
            return QQMediaResult(images=tuple(materialized))
        except asyncio.CancelledError:
            await asyncio.to_thread(_remove_paths, created)
            raise
        except _ImageTooLargeError:
            await asyncio.to_thread(_remove_paths, created)
            return QQMediaResult(input_error=IMAGE_TOO_LARGE)
        except _UnsupportedImageError:
            await asyncio.to_thread(_remove_paths, created)
            return QQMediaResult(input_error=UNSUPPORTED_IMAGE)
        except _InvalidImageError:
            await asyncio.to_thread(_remove_paths, created)
            return QQMediaResult(input_error=INVALID_IMAGE)
        except Exception:
            await asyncio.to_thread(_remove_paths, created)
            return QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)

    async def _download_image(
        self,
        reference: QQImageReference,
        *,
        remaining_quota: int,
    ) -> QQMaterializedImage:
        url = _normalize_media_url(reference.url)
        part_path: Path | None = None
        final_path: Path | None = None
        descriptor: int | None = None
        header = bytearray()
        size_bytes = 0
        try:
            async with self.http_client.stream(
                "GET",
                url,
                follow_redirects=False,
                timeout=_DOWNLOAD_TIMEOUT,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise _MediaDownloadError
                if not response.is_success:
                    raise _MediaDownloadError

                descriptor, part_path = await asyncio.to_thread(self._open_private_part)
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    size_bytes += len(chunk)
                    if size_bytes > MAX_IMAGE_BYTES:
                        raise _ImageTooLargeError
                    if size_bytes > remaining_quota:
                        raise _MediaDownloadError
                    if len(header) < 16:
                        header.extend(chunk[: 16 - len(header)])
                    await asyncio.to_thread(_write_all, descriptor, chunk)

            if descriptor is not None:
                await asyncio.to_thread(os.close, descriptor)
                descriptor = None
            content_type, extension = _sniff_image(bytes(header))
            assert part_path is not None
            await asyncio.to_thread(_verify_image_file, part_path, content_type)
            final_path = part_path.with_suffix(extension)
            await asyncio.to_thread(os.replace, part_path, final_path)
            part_path = None
            await asyncio.to_thread(_chmod_private_file, final_path)
            result = QQMaterializedImage(
                content_type=content_type,
                local_path=str(final_path),
                size_bytes=size_bytes,
            )
            final_path = None
            return result
        except asyncio.CancelledError:
            raise
        except (
            _ImageTooLargeError,
            _UnsupportedImageError,
            _InvalidImageError,
            _MediaDownloadError,
        ):
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _MediaDownloadError from exc
        finally:
            if descriptor is not None:
                try:
                    await asyncio.to_thread(os.close, descriptor)
                except OSError:
                    pass
            await asyncio.to_thread(
                _remove_paths,
                [path for path in (part_path, final_path) if path is not None],
            )

    def _open_private_part(self) -> tuple[int, Path]:
        token = secrets.token_hex(16)
        path = self.root / f"{token}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            _chmod_private_file(path)
            return descriptor, path
        except BaseException:
            try:
                os.close(descriptor)
            finally:
                _remove_paths([path])
            raise

    def _prepare_and_sweep(self, create: bool) -> int:
        if _is_reparse_path(self.root):
            raise RuntimeError("QQ inbound media directory must not be a symlink or junction")
        if not self.root.exists() and not create:
            return 0
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_path(self.root) or not self.root.is_dir():
            raise RuntimeError("QQ inbound media path must be a directory")
        _chmod_private_directory(self.root)
        root_identity = _path_identity(self.root)

        cutoff = self.clock() - MEDIA_RETENTION_S
        for path in self.root.iterdir():
            try:
                if _is_reparse_path(path):
                    raise RuntimeError("QQ inbound media spool contains a symlink or junction")
                if not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_mtime <= cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue

        usage = 0
        for path in self.root.iterdir():
            try:
                if _is_reparse_path(path):
                    raise RuntimeError("QQ inbound media spool contains a symlink or junction")
                if path.is_file():
                    usage += path.stat().st_size
            except FileNotFoundError:
                continue
        if _is_reparse_path(self.root) or _path_identity(self.root) != root_identity:
            raise RuntimeError("QQ inbound media directory changed during cleanup")
        return usage


def _normalize_media_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized or any(character.isspace() for character in normalized):
        raise _MediaDownloadError
    try:
        parsed = urlsplit(normalized)
        host = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise _MediaDownloadError from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not host:
        raise _MediaDownloadError
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise _MediaDownloadError
    if port not in (None, 443):
        raise _MediaDownloadError
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _TRUSTED_MEDIA_HOSTS):
        raise _MediaDownloadError
    return normalized


def _sniff_image(header: bytes) -> tuple[str, str]:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise _UnsupportedImageError


def _verify_image_file(path: Path, expected_content_type: str) -> None:
    expected_format = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
    }[expected_content_type]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                detected_format = image.format
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise _InvalidImageError
                if width * height > MAX_IMAGE_PIXELS:
                    raise _ImageTooLargeError
                image.verify()
            # Pillow's JPEG and WebP verify implementations do not decode the
            # pixel stream. Reopen and load so truncated entropy/chunk data is
            # rejected before the file is published to the spool.
            with Image.open(path) as image:
                image.load()
    except (_ImageTooLargeError, _InvalidImageError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise _ImageTooLargeError from exc
    except Exception as exc:
        raise _InvalidImageError from exc
    if detected_format != expected_format:
        raise _InvalidImageError


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("QQ inbound media write did not make progress")
        view = view[written:]


def _remove_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _chmod_private_directory(path: Path) -> None:
    if os.name == "nt":
        secure_windows_path(path, directory=True)
    else:
        os.chmod(path, 0o700)


def _chmod_private_file(path: Path) -> None:
    if os.name == "nt":
        secure_windows_path(path)
    else:
        os.chmod(path, 0o600)


def _is_reparse_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        return bool(getattr(path.lstat(), "st_reparse_tag", 0))
    except FileNotFoundError:
        return False


def _path_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return int(info.st_dev), int(info.st_ino)

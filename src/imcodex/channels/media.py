from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import logging
import multiprocessing
import os
from pathlib import Path
import secrets
import time
from typing import Awaitable, Callable, Generic, TypeVar
import warnings

from PIL import Image

from ..models import InboundAttachment, InboundMessage
from ..observability.runtime import emit_event
from ..windows_security import secure_windows_path


logger = logging.getLogger(__name__)

MAX_IMAGE_COUNT = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MEDIA_RETENTION_S = 24 * 60 * 60
MEDIA_QUOTA_BYTES = 512 * 1024 * 1024
MEDIA_MAX_SPOOL_ENTRIES = 16_384
MEDIA_MATERIALIZE_DEADLINE_S = 30.0
MEDIA_CLEANUP_INTERVAL_S = 60 * 60
MEDIA_LOCK_POLL_INTERVAL_S = 0.05
MEDIA_LOCK_FILE_NAME = ".imcodex-spool.lock"
MEDIA_PROCESS_TERMINATE_S = 1.0
MEDIA_CANCELLATION_CLEANUP_S = 2.0

IMAGE_TOO_LARGE = "image_too_large"
TOO_MANY_IMAGES = "too_many_images"
UNSUPPORTED_IMAGE = "unsupported_image"
INVALID_IMAGE = "invalid_image"
IMAGE_DOWNLOAD_FAILED = "image_download_failed"
MEDIA_SPOOL_UNAVAILABLE_MESSAGE = "Inbound media staging is unavailable."


class ImageTooLargeError(Exception):
    pass


class UnsupportedImageError(Exception):
    pass


class InvalidImageError(Exception):
    pass


class MediaDownloadError(Exception):
    pass


class MediaSpoolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MaterializedImage:
    content_type: str
    local_path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class MediaResult:
    images: tuple[MaterializedImage, ...] = ()
    input_error: str | None = None


@dataclass(frozen=True, slots=True)
class _BufferedImage:
    content_type: str
    extension: str
    data: bytes
    token: str


ImageReferenceT = TypeVar("ImageReferenceT")
ChunkWriter = Callable[[bytes], Awaitable[None]]
ImageDownloader = Callable[[ImageReferenceT, ChunkWriter], Awaitable[None]]


class ImageMediaMaterializer(Generic[ImageReferenceT]):
    """Validate and privately stage channel-supplied image byte streams.

    Channel adapters own authentication and media retrieval. This class owns
    the shared trust boundary after retrieval: byte and pixel limits, actual
    format detection, full decode validation, private randomized files, spool
    quota, expiry, and a whole-batch deadline.
    """

    def __init__(
        self,
        *,
        root: Path,
        download: ImageDownloader[ImageReferenceT],
        clock: Callable[[], float] = time.time,
        cleanup_sleep=asyncio.sleep,
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.download = download
        self.clock = clock
        self.cleanup_sleep = cleanup_sleep
        # The asyncio lock preserves in-process ordering. The killable staging
        # worker owns the filesystem lock for cross-instance/process quota
        # transactions; the event loop never owns a filesystem descriptor.
        self._spool_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._active_processes: set[multiprocessing.Process] = set()
        # Only workers whose owning coroutine could not terminate them are
        # eligible for cross-call reaping. Normal active workers remain owned
        # exclusively by their `_run_process()` coroutine until its `finally`.
        self._retained_processes: set[multiprocessing.Process] = set()
        self._tainted = False
        self._closed = False

    async def start(self) -> None:
        self._closed = False
        await self.prepare()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._run_cleanup())

    async def stop(self) -> None:
        self._closed = True
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        failures: list[Exception] = []
        for process in tuple(self._active_processes):
            try:
                await self._terminate_owned_process(process)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Inbound media worker shutdown failed", failures)

    async def prepare(self) -> None:
        try:
            self._reap_processes()
            if self._tainted:
                raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
            async with asyncio.timeout(MEDIA_MATERIALIZE_DEADLINE_S):
                async with self._spool_lock:
                    status, _images = await self._run_stage_worker(
                        (),
                        create=False,
                    )
                    if status != "ok":
                        raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Filesystem exceptions often embed the absolute spool or sibling
            # lock path. Startup diagnostics may render this exception, so
            # collapse it at the media boundary and suppress its context.
            raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE) from None

    async def materialize(
        self,
        references: tuple[ImageReferenceT, ...],
    ) -> MediaResult:
        if not references:
            return MediaResult()
        if len(references) > MAX_IMAGE_COUNT:
            return MediaResult(input_error=TOO_MANY_IMAGES)
        self._reap_processes()
        if self._tainted:
            return MediaResult(input_error=IMAGE_DOWNLOAD_FAILED)

        try:
            async with asyncio.timeout(MEDIA_MATERIALIZE_DEADLINE_S):
                async with self._spool_lock:
                    preflight_status, _images = await self._run_stage_worker(
                        (),
                        create=True,
                        require_capacity=True,
                    )
                    if preflight_status != "ok":
                        return MediaResult(input_error=preflight_status)
                    buffered = await self._download_images(references)
                    status, images = await self._run_stage_worker(
                        buffered,
                        create=True,
                    )
                if status == "ok":
                    return MediaResult(images=images)
                return MediaResult(input_error=status)
        except ImageTooLargeError:
            return MediaResult(input_error=IMAGE_TOO_LARGE)
        except UnsupportedImageError:
            return MediaResult(input_error=UNSUPPORTED_IMAGE)
        except InvalidImageError:
            return MediaResult(input_error=INVALID_IMAGE)
        except TimeoutError:
            return MediaResult(input_error=IMAGE_DOWNLOAD_FAILED)
        except Exception:
            return MediaResult(input_error=IMAGE_DOWNLOAD_FAILED)

    async def _download_images(
        self,
        references: tuple[ImageReferenceT, ...],
    ) -> tuple[_BufferedImage, ...]:
        buffered: list[_BufferedImage] = []
        for reference in references:
            data = bytearray()

            async def write_chunk(chunk: bytes) -> None:
                if not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                if not chunk:
                    return
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ImageTooLargeError
                data.extend(chunk)

            await self.download(reference, write_chunk)
            content_type, extension = _sniff_image(bytes(data[:16]))
            buffered.append(
                _BufferedImage(
                    content_type=content_type,
                    extension=extension,
                    data=bytes(data),
                    token=secrets.token_hex(16),
                )
            )
        return tuple(buffered)

    def _try_process_lock(self, create: bool) -> tuple[str, int | None]:
        return _try_process_lock_path(self.root, create=create)

    async def _run_stage_worker(
        self,
        images: tuple[_BufferedImage, ...],
        *,
        create: bool,
        require_capacity: bool = False,
    ) -> tuple[str, tuple[MaterializedImage, ...]]:
        known_paths = tuple(
            path
            for image in images
            for path in (
                self.root / f"{image.token}.part",
                self.root / f"{image.token}{image.extension}",
            )
        )
        try:
            payload = await self._run_process(
                target=_stage_batch_worker,
                args=(
                    str(self.root),
                    create,
                    self.clock() - MEDIA_RETENTION_S,
                    MEDIA_MAX_SPOOL_ENTRIES,
                    MEDIA_QUOTA_BYTES,
                    MAX_IMAGE_PIXELS,
                    images,
                    require_capacity,
                ),
                name="imcodex-media-stage",
            )
        except BaseException:
            if known_paths:
                try:
                    await self._cleanup_known_paths(known_paths)
                except MediaSpoolError:
                    # Preserve cancellation and the original worker failure;
                    # cleanup already marked this materializer fail-closed.
                    pass
            raise
        try:
            if not isinstance(payload, tuple) or len(payload) != 2:
                raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
            status, raw_images = payload
            if not isinstance(raw_images, tuple):
                raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
        except BaseException:
            if known_paths:
                try:
                    await self._cleanup_known_paths(known_paths)
                except MediaSpoolError:
                    pass
            raise
        if status != "ok":
            if known_paths:
                # Reconfirm rollback in a separate bounded transaction. The
                # stage child may have been interrupted while reporting an
                # image validation or filesystem failure.
                await self._cleanup_known_paths(known_paths)
            return str(status), ()
        materialized = tuple(
            MaterializedImage(
                content_type=str(content_type),
                local_path=str(local_path),
                size_bytes=int(size_bytes),
            )
            for content_type, local_path, size_bytes in raw_images
        )
        return "ok", materialized

    async def _cleanup_known_paths(self, paths: tuple[Path, ...]) -> None:
        try:
            async with asyncio.timeout(MEDIA_CANCELLATION_CLEANUP_S):
                payload = await self._run_process(
                    target=_cleanup_paths_worker,
                    args=(str(self.root), tuple(str(path) for path in paths)),
                    name="imcodex-media-cleanup",
                )
            if payload != ("ok", ()):
                raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
        except BaseException as exc:
            self._tainted = True
            logger.error(
                "Inbound media cancellation cleanup failed: %s",
                type(exc).__name__,
            )
            raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE) from None

    async def _run_process(
        self,
        *,
        target,
        args: tuple,
        name: str,
    ):
        context = multiprocessing.get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=target,
            args=(*args, send_connection),
            name=name,
            daemon=True,
        )
        started = False
        try:
            process.start()
            started = True
            self._active_processes.add(process)
            send_connection.close()
            while process.is_alive():
                await asyncio.sleep(0.01)
            process.join()
            if process.exitcode != 0 or not receive_connection.poll():
                raise MediaSpoolError(MEDIA_SPOOL_UNAVAILABLE_MESSAGE)
            return receive_connection.recv()
        except asyncio.CancelledError:
            if started:
                await self._terminate_owned_process(process)
            raise
        except BaseException:
            if started and process.is_alive():
                await self._terminate_owned_process(process)
            raise
        finally:
            try:
                send_connection.close()
            except OSError:
                pass
            try:
                receive_connection.close()
            except OSError:
                pass
            if started:
                self._close_finished_owned_process(process)

    async def _terminate_owned_process(self, process) -> None:
        was_retained = process in self._retained_processes
        # Claim a previously retained handle synchronously before the first
        # await so `_reap_processes()` cannot close it mid-termination.
        if was_retained:
            self._retained_processes.discard(process)
        try:
            alive = process.is_alive()
        except ValueError:
            self._active_processes.discard(process)
            return
        if not alive:
            process.join()
            self._active_processes.discard(process)
            # A normal completed worker is still owned by `_run_process()`,
            # which must consume its result pipe and inspect `exitcode` before
            # closing the handle. Only a previously retained orphan can be
            # closed by a later shutdown/reap caller.
            if was_retained:
                try:
                    process.close()
                except (OSError, ValueError):
                    pass
            return
        try:
            await _terminate_process(process)
        except ValueError:
            # The `_run_process()` owner may observe exit and close the handle
            # while shutdown is awaiting termination. A closed handle proves
            # ownership cleanup already completed; it is not a spool failure.
            self._active_processes.discard(process)
            self._retained_processes.discard(process)
            return
        except BaseException:
            self._tainted = True
            self._retained_processes.add(process)
            raise
        try:
            alive = process.is_alive()
        except ValueError:
            self._active_processes.discard(process)
            self._retained_processes.discard(process)
            return
        if alive:
            self._tainted = True
            self._retained_processes.add(process)
            raise MediaSpoolError("Inbound media worker could not be terminated.")
        process.join()
        self._active_processes.discard(process)
        self._retained_processes.discard(process)
        if was_retained:
            try:
                process.close()
            except (OSError, ValueError):
                pass

    def _reap_processes(self) -> None:
        for process in tuple(self._retained_processes):
            try:
                alive = process.is_alive()
            except ValueError:
                # Defensive recovery for a handle closed by external teardown.
                self._retained_processes.discard(process)
                self._active_processes.discard(process)
                continue
            if alive:
                continue
            process.join()
            self._retained_processes.discard(process)
            self._active_processes.discard(process)
            try:
                process.close()
            except (OSError, ValueError):
                pass
        # A normal worker is never inspected here: its owner may still be
        # between observing process exit and consuming the result pipe.

    def _close_finished_owned_process(self, process) -> bool:
        """Close a completed child while clearing every ownership registry."""

        try:
            if process.is_alive():
                return False
        except ValueError:
            self._retained_processes.discard(process)
            self._active_processes.discard(process)
            return True
        process.join()
        self._retained_processes.discard(process)
        self._active_processes.discard(process)
        try:
            process.close()
        except (OSError, ValueError):
            pass
        return True

    async def _run_cleanup(self) -> None:
        while not self._closed:
            await self.cleanup_sleep(MEDIA_CLEANUP_INTERVAL_S)
            if self._closed:
                return
            try:
                await self.prepare()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Inbound media cleanup failed: %s",
                    type(exc).__name__,
                )


def _stage_batch_worker(
    root_value: str,
    create: bool,
    cutoff: float,
    max_entries: int,
    quota_bytes: int,
    max_pixels: int,
    images: tuple[_BufferedImage, ...],
    require_capacity: bool,
    connection,
) -> None:
    root = Path(root_value)
    descriptor: int | None = None
    all_paths = [
        path
        for image in images
        for path in (
            root / f"{image.token}.part",
            root / f"{image.token}{image.extension}",
        )
    ]
    result: tuple[str, tuple[tuple[str, str, int], ...]]
    try:
        while True:
            lock_status, descriptor = _try_process_lock_path(root, create=create)
            if lock_status == "missing":
                result = ("ok", ())
                break
            if lock_status == "acquired":
                assert descriptor is not None
                usage = _prepare_and_sweep_path(
                    root,
                    create=create,
                    cutoff=cutoff,
                    max_entries=max_entries,
                )
                if require_capacity and usage >= quota_bytes:
                    raise MediaDownloadError
                if usage + sum(len(image.data) for image in images) > quota_bytes:
                    raise MediaDownloadError
                staged: list[tuple[str, str, int]] = []
                for image in images:
                    part_path = root / f"{image.token}.part"
                    final_path = root / f"{image.token}{image.extension}"
                    _stage_buffered_image(
                        part_path,
                        final_path,
                        image,
                        max_pixels=max_pixels,
                    )
                    usage += len(image.data)
                    staged.append(
                        (image.content_type, str(final_path), len(image.data))
                    )
                result = ("ok", tuple(staged))
                break
            time.sleep(MEDIA_LOCK_POLL_INTERVAL_S)
    except ImageTooLargeError:
        _remove_paths(all_paths)
        result = (IMAGE_TOO_LARGE, ())
    except UnsupportedImageError:
        _remove_paths(all_paths)
        result = (UNSUPPORTED_IMAGE, ())
    except InvalidImageError:
        _remove_paths(all_paths)
        result = (INVALID_IMAGE, ())
    except BaseException:
        _remove_paths(all_paths)
        result = (IMAGE_DOWNLOAD_FAILED, ())
    finally:
        if descriptor is not None:
            try:
                _release_process_lock(descriptor)
            except BaseException:
                _remove_paths(all_paths)
                result = (IMAGE_DOWNLOAD_FAILED, ())
        try:
            connection.send(result)
        finally:
            connection.close()


def _cleanup_paths_worker(
    root_value: str,
    path_values: tuple[str, ...],
    connection,
) -> None:
    root = Path(root_value)
    descriptor: int | None = None
    result = ("error", ())
    try:
        while True:
            status, descriptor = _try_process_lock_path(root, create=False)
            if status == "missing":
                result = ("ok", ())
                break
            if status == "acquired":
                assert descriptor is not None
                _remove_paths_strict([Path(value) for value in path_values])
                result = ("ok", ())
                break
            time.sleep(MEDIA_LOCK_POLL_INTERVAL_S)
    except BaseException:
        result = ("error", ())
    finally:
        if descriptor is not None:
            try:
                _release_process_lock(descriptor)
            except BaseException:
                result = ("error", ())
        try:
            connection.send(result)
        finally:
            connection.close()


def _stage_buffered_image(
    part_path: Path,
    final_path: Path,
    image: _BufferedImage,
    *,
    max_pixels: int,
) -> None:
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(part_path, flags, 0o600)
        _chmod_private_file(part_path)
        _write_all(descriptor, image.data)
        os.close(descriptor)
        descriptor = None
        _verify_image_file(
            part_path,
            image.content_type,
            max_pixels=max_pixels,
        )
        os.replace(part_path, final_path)
        _chmod_private_file(final_path)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _try_process_lock_path(
    root: Path,
    *,
    create: bool,
) -> tuple[str, int | None]:
    if _is_reparse_path(root):
        raise RuntimeError("Inbound media directory must not be a symlink or junction")
    if not root.exists():
        if not create:
            return "missing", None
        root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_path(root) or not root.is_dir():
        raise RuntimeError("Inbound media path must be a directory")
    _chmod_private_directory(root)
    root_identity = _path_identity(root)

    lock_path = root.parent / f".{root.name}{MEDIA_LOCK_FILE_NAME}"
    if _is_reparse_path(lock_path):
        raise RuntimeError("Inbound media lock must not be a symlink or junction")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        _chmod_private_file(lock_path)
        if _is_reparse_path(lock_path):
            raise RuntimeError("Inbound media lock must not be a symlink or junction")
        if _is_reparse_path(root) or _path_identity(root) != root_identity:
            raise RuntimeError("Inbound media directory changed while locking")
        if _try_lock_descriptor(descriptor):
            return "acquired", descriptor
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return "busy", None


def _prepare_and_sweep_path(
    root: Path,
    *,
    create: bool,
    cutoff: float,
    max_entries: int,
) -> int:
    if _is_reparse_path(root):
        raise RuntimeError("Inbound media directory must not be a symlink or junction")
    if not root.exists() and not create:
        return 0
    root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_path(root) or not root.is_dir():
        raise RuntimeError("Inbound media path must be a directory")
    _chmod_private_directory(root)
    root_identity = _path_identity(root)

    usage = 0
    entry_count = 0
    for path in root.iterdir():
        entry_count += 1
        if entry_count > max_entries:
            raise RuntimeError("Inbound media spool contains too many entries")
        try:
            if _is_reparse_path(path):
                raise RuntimeError("Inbound media spool contains a symlink or junction")
            if not path.is_file():
                continue
            stat_result = path.stat()
            if stat_result.st_mtime <= cutoff:
                path.unlink()
                continue
            usage += stat_result.st_size
        except FileNotFoundError:
            continue
    if _is_reparse_path(root) or _path_identity(root) != root_identity:
        raise RuntimeError("Inbound media directory changed during cleanup")
    return usage


async def materialize_inbound_images(
    inbound: InboundMessage,
    references: tuple[ImageReferenceT, ...],
    materializer: ImageMediaMaterializer[ImageReferenceT],
) -> InboundMessage:
    """Apply the shared staged-image result without exposing platform secrets."""

    emit_event(
        component=f"channels.{inbound.channel_id}",
        event="message.inbound.media.materializing",
        message="Inbound image materialization started",
        channel_id=inbound.channel_id,
        conversation_id=inbound.conversation_id,
        user_id=inbound.user_id,
        message_id=inbound.message_id,
        data={"attachment_count": len(references)},
    )
    result = await materializer.materialize(references)
    inbound.attachments = tuple(
        InboundAttachment(
            kind="image",
            content_type=image.content_type,
            local_path=image.local_path,
            size_bytes=image.size_bytes,
        )
        for image in result.images
    )
    inbound.input_error = result.input_error
    if result.input_error is not None:
        emit_event(
            component=f"channels.{inbound.channel_id}",
            event="message.inbound.media.rejected",
            level="WARNING",
            message="Inbound image materialization rejected",
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            user_id=inbound.user_id,
            message_id=inbound.message_id,
            data={
                "attachment_count": len(references),
                "error_code": result.input_error,
            },
        )
    elif result.images:
        emit_event(
            component=f"channels.{inbound.channel_id}",
            event="message.inbound.media.materialized",
            message="Inbound image materialization completed",
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            user_id=inbound.user_id,
            message_id=inbound.message_id,
            data={
                "attachment_count": len(result.images),
                "content_types": [image.content_type for image in result.images],
                "size_bytes": [image.size_bytes for image in result.images],
            },
        )
    return inbound


def _sniff_image(header: bytes) -> tuple[str, str]:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise UnsupportedImageError


async def _terminate_process(process) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    if await _wait_for_process_exit(process, MEDIA_PROCESS_TERMINATE_S):
        process.join()
        return
    if hasattr(process, "kill"):
        process.kill()
    if await _wait_for_process_exit(process, MEDIA_PROCESS_TERMINATE_S):
        process.join()
        return
    raise MediaSpoolError("Inbound media worker could not be terminated.")


async def _wait_for_process_exit(process, timeout_s: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while process.is_alive() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return not process.is_alive()


def _verify_image_file(
    path: Path,
    expected_content_type: str,
    *,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> None:
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
                    raise InvalidImageError
                if width * height > max_pixels:
                    raise ImageTooLargeError
                if int(getattr(image, "n_frames", 1)) != 1:
                    raise UnsupportedImageError
                image.verify()
            # JPEG and WebP verify do not fully decode their pixel streams.
            with Image.open(path) as image:
                image.load()
    except (ImageTooLargeError, UnsupportedImageError, InvalidImageError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError from exc
    except Exception as exc:
        raise InvalidImageError from exc
    if detected_format != expected_format:
        raise InvalidImageError


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Inbound media write did not make progress")
        view = view[written:]


def _try_lock_descriptor(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                exc,
                "winerror",
                None,
            ) in {33, 36}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _release_process_lock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _remove_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _remove_paths_strict(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
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

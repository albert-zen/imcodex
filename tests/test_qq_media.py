from __future__ import annotations

import asyncio
from io import BytesIO
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import httpx
from PIL import Image
import pytest

from imcodex.channels.access import ChannelAccessPolicy
from imcodex.channels.media import (
    ImageMediaMaterializer,
    MEDIA_SPOOL_UNAVAILABLE_MESSAGE,
    MediaSpoolError,
    _BufferedImage,
)
from imcodex.channels.qq import QQChannelAdapter
from imcodex.channels.qq_media import (
    IMAGE_DOWNLOAD_FAILED,
    IMAGE_TOO_LARGE,
    INVALID_IMAGE,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MEDIA_QUOTA_BYTES,
    MEDIA_RETENTION_S,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE,
    QQImageReference,
    QQMaterializedImage,
    QQMediaMaterializer,
    QQMediaResult,
    parse_qq_image_references,
)


def _encoded_image(image_format: str) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(stream, format=image_format)
    return stream.getvalue()


def _animated_png() -> bytes:
    stream = BytesIO()
    frames = [
        Image.new("RGB", (2, 2), (20, 40, 60)),
        Image.new("RGB", (2, 2), (60, 40, 20)),
    ]
    frames[0].save(
        stream,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return stream.getvalue()


PNG = _encoded_image("PNG")
JPEG = _encoded_image("JPEG")
WEBP = _encoded_image("WEBP")
ANIMATED_PNG = _animated_png()
TRUSTED_URL = "https://multimedia.nt.qq.com.cn/download/image?token=secret"


def _stage_then_block_worker(
    root_value: str,
    _create: bool,
    _cutoff: float,
    _max_entries: int,
    _quota_bytes: int,
    _max_pixels: int,
    images: tuple[_BufferedImage, ...],
    _require_capacity: bool,
    connection,
) -> None:
    """Test child that reaches final rename but never reports completion."""

    root = Path(root_value)
    root.mkdir(parents=True, exist_ok=True)
    for image in images:
        (root / f"{image.token}{image.extension}").write_bytes(image.data)
    time.sleep(60)
    connection.close()


def _reference(
    url: str = TRUSTED_URL,
) -> QQImageReference:
    return QQImageReference(url=url)


def _assert_no_spooled_files(root: Path) -> None:
    assert not root.exists() or not any(root.iterdir())


def test_parse_qq_image_references_ignores_declared_metadata_and_bounds_input() -> None:
    references = parse_qq_image_references(
        [
            {
                "content_type": "application/octet-stream",
                "filename": "../../secret.png",
                "size": str(MAX_IMAGE_BYTES + 1),
                "url": TRUSTED_URL,
            },
            {"url": TRUSTED_URL},
            *[
                {"contentType": "application/pdf", "url": TRUSTED_URL}
                for _ in range(10)
            ],
        ]
    )

    assert len(references) == 5
    assert references[0].url == TRUSTED_URL
    assert all("secret.png" not in reference.url for reference in references)


@pytest.mark.asyncio
async def test_materializer_prepare_does_not_create_an_empty_spool(tmp_path: Path) -> None:
    root = tmp_path / "media"

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("prepare must not access the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        await materializer.prepare()

    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_type", "expected_suffix"),
    [
        (PNG, "image/png", ".png"),
        (JPEG, "image/jpeg", ".jpg"),
        (WEBP, "image/webp", ".webp"),
    ],
)
async def test_materializer_downloads_supported_images_to_private_absolute_paths(
    tmp_path: Path,
    body: bytes,
    expected_type: str,
    expected_suffix: str,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body, headers={"Content-Type": "application/octet-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        await materializer.prepare()
        result = await materializer.materialize((_reference(),))

    assert result.input_error is None
    assert len(result.images) == 1
    image = result.images[0]
    path = Path(image.local_path)
    assert path.is_absolute()
    assert path.parent == (tmp_path / "media").absolute()
    assert path.suffix == expected_suffix
    assert "secret" not in path.name
    assert image.content_type == expected_type
    assert image.size_bytes == len(body)
    assert path.read_bytes() == body
    assert requests[0].url.host == "multimedia.nt.qq.com.cn"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert not list(path.parent.glob("*.part"))
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"\x89PNG\r\n\x1a\nnot-a-png",
        PNG[:-8],
        JPEG[: len(JPEG) // 2],
        JPEG[:-1],
        WEBP[:-4],
        WEBP[:-1],
    ],
)
async def test_materializer_rejects_malformed_supported_image_content(
    tmp_path: Path,
    body: bytes,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(root=root, http_client=client).materialize(
            (_reference(),)
        )

    assert result == QQMediaResult(input_error=INVALID_IMAGE)
    _assert_no_spooled_files(root)


@pytest.mark.asyncio
async def test_materializer_rejects_animated_images_before_native_submission(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ANIMATED_PNG)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(root=root, http_client=client).materialize(
            (_reference(),)
        )

    assert result == QQMediaResult(input_error=UNSUPPORTED_IMAGE)
    _assert_no_spooled_files(root)


@pytest.mark.asyncio
async def test_materializer_rejects_excessive_decoded_pixel_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.media.MAX_IMAGE_PIXELS", 3)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(root=root, http_client=client).materialize(
            (_reference(),)
        )

    assert MAX_IMAGE_PIXELS > 3
    assert result == QQMediaResult(input_error=IMAGE_TOO_LARGE)
    _assert_no_spooled_files(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACLs only")
@pytest.mark.asyncio
async def test_materializer_replaces_windows_spool_dacl(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    subprocess.run(
        ["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)(R)"],
        capture_output=True,
        text=True,
        check=True,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(root=root, http_client=client).materialize((_reference(),))

    powershell = shutil.which("pwsh") or "powershell.exe"

    def access_sids(path: Path) -> set[str]:
        script = (
            "$acl = Get-Acl -LiteralPath $env:IMCODEX_TEST_MEDIA_PATH; "
            "$acl.Access | ForEach-Object { "
            "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
            "}"
        )
        environment = os.environ.copy()
        environment["IMCODEX_TEST_MEDIA_PATH"] = str(path)
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-Command", script],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return {line.strip() for line in completed.stdout.splitlines() if line.strip()}

    assert result.input_error is None
    for path in (root, Path(result.images[0].local_path)):
        sids = access_sids(path)
        assert "S-1-1-0" not in sids
        assert len(sids) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
@pytest.mark.asyncio
async def test_materializer_rejects_windows_junction_root_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    victim = target / "old.png"
    victim.write_bytes(PNG)
    root = tmp_path / "media"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(root), str(target)],
        capture_output=True,
        text=True,
        check=True,
    )

    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        with pytest.raises(MediaSpoolError) as captured:
            await materializer.prepare()

    assert str(captured.value) == MEDIA_SPOOL_UNAVAILABLE_MESSAGE
    assert str(tmp_path) not in str(captured.value)
    assert victim.read_bytes() == PNG


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
@pytest.mark.asyncio
async def test_materializer_rejects_existing_root_beneath_windows_junction(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    root_target = target / "media"
    root_target.mkdir(parents=True)
    victim = root_target / "old.png"
    victim.write_bytes(PNG)
    junction = tmp_path / "redirected"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=True,
    )

    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(
            root=junction / "media",
            http_client=client,
        )
        with pytest.raises(MediaSpoolError):
            await materializer.prepare()

    assert victim.read_bytes() == PNG


@pytest.mark.asyncio
async def test_materializer_accepts_protocol_relative_trusted_url(tmp_path: Path) -> None:
    seen_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize(
            (_reference("//multimedia.nt.qq.com/image.png?token=secret"),)
        )

    assert result.input_error is None
    assert seen_urls == ["https://multimedia.nt.qq.com/image.png?token=secret"]


@pytest.mark.asyncio
async def test_materializer_rejects_count_before_network(tmp_path: Path) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        too_many = await materializer.materialize(tuple(_reference() for _ in range(5)))

    assert too_many.input_error == TOO_MANY_IMAGES
    assert requests == 0


@pytest.mark.asyncio
async def test_materializer_uses_downloaded_content_not_declared_metadata(tmp_path: Path) -> None:
    references = parse_qq_image_references(
        [
            {
                "content_type": "application/octet-stream",
                "size": str(MAX_IMAGE_BYTES + 1),
                "url": TRUSTED_URL,
            }
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PNG,
            headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize(references)

    assert result.input_error is None
    assert result.images[0].content_type == "image/png"
    assert result.images[0].size_bytes == len(PNG)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://multimedia.nt.qq.com/image.png",
        "https://user:password@multimedia.nt.qq.com/image.png",
        "https://multimedia.nt.qq.com:8443/image.png",
        "https://example.com/image.png",
    ],
)
async def test_materializer_rejects_unsafe_urls_without_network(tmp_path: Path, url: str) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize((_reference(url),))

    assert result == QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)
    assert requests == 0


@pytest.mark.asyncio
async def test_materializer_rejects_redirect_without_following_it(tmp_path: Path) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://multimedia.nt.qq.com/other.png?token=other-secret"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize((_reference(),))

    assert result.input_error == IMAGE_DOWNLOAD_FAILED
    assert requests == [TRUSTED_URL]
    _assert_no_spooled_files(tmp_path / "media")


@pytest.mark.asyncio
async def test_materializer_rejects_http_content_encoding_before_decoding(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            content=PNG,
            headers={"Content-Encoding": "gzip"},
        )

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(root=root, http_client=client).materialize(
            (_reference(),)
        )

    assert result == QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)
    _assert_no_spooled_files(root)


class _OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield PNG
        block = b"x" * (1024 * 1024)
        for _ in range(10):
            yield block


@pytest.mark.asyncio
async def test_materializer_enforces_stream_limit_and_removes_partial_file(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_OversizedStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize((_reference(),))

    assert result.input_error == IMAGE_TOO_LARGE
    _assert_no_spooled_files(tmp_path / "media")


@pytest.mark.asyncio
async def test_materializer_rejects_unknown_magic_and_cleans_other_images_from_message(
    tmp_path: Path,
) -> None:
    responses = iter((PNG, b"not-an-image"))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await materializer.materialize((_reference(), _reference()))

    assert result.input_error == UNSUPPORTED_IMAGE
    assert result.images == ()
    _assert_no_spooled_files(tmp_path / "media")


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield PNG
        self.started.set()
        await self.release.wait()
        yield b"done"


@pytest.mark.asyncio
async def test_materializer_cancellation_removes_partial_file(tmp_path: Path) -> None:
    stream = _BlockingStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        task = asyncio.create_task(materializer.materialize((_reference(),)))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _assert_no_spooled_files(tmp_path / "media")


class _NeverEndingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield PNG
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_materializer_has_a_whole_message_deadline_and_cleans_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.media.MEDIA_MATERIALIZE_DEADLINE_S", 0.01)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_NeverEndingStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        result = await asyncio.wait_for(
            materializer.materialize((_reference(),)),
            timeout=1,
        )

    assert result.input_error == IMAGE_DOWNLOAD_FAILED
    _assert_no_spooled_files(tmp_path / "media")


@pytest.mark.asyncio
async def test_materializer_deadline_includes_waiting_for_in_process_spool_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.media.MEDIA_MATERIALIZE_DEADLINE_S", 0.01)
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=tmp_path / "media", http_client=client)
        await materializer._spool_lock.acquire()
        try:
            result = await asyncio.wait_for(
                materializer.materialize((_reference(),)),
                timeout=0.5,
            )
        finally:
            materializer._spool_lock.release()

    assert result == QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)
    assert requests == 0


@pytest.mark.asyncio
async def test_isolated_spool_stage_is_terminated_when_deadline_cancels(
    tmp_path: Path,
) -> None:
    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(
            root=tmp_path / "media",
            http_client=client,
        )
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await materializer._run_stage_worker((), create=True)

    assert all(
        process.name != "imcodex-media-stage"
        for process in multiprocessing.active_children()
    )


@pytest.mark.asyncio
async def test_stage_cancellation_rolls_back_files_renamed_before_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "imcodex.channels.media._stage_batch_worker",
        _stage_then_block_worker,
    )
    root = tmp_path / "media"
    token = "cancelled-transaction"
    image = _BufferedImage(
        content_type="image/png",
        extension=".png",
        data=PNG,
        token=token,
    )

    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        task = asyncio.create_task(
            materializer._run_stage_worker((image,), create=True)
        )
        final_path = root / f"{token}.png"
        for _ in range(500):
            if final_path.exists() or task.done():
                break
            await asyncio.sleep(0.01)
        assert final_path.exists()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _assert_no_spooled_files(root)
    assert materializer._active_processes == set()
    assert materializer._tainted is False


class _UnkillableProcess:
    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def join(self) -> None:
        raise AssertionError("a live worker must not be joined")


class _ExitedOwnedProcess:
    def __init__(self) -> None:
        self.join_calls = 0
        self.close_calls = 0

    def is_alive(self) -> bool:
        return False

    def join(self) -> None:
        self.join_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _ClosingOwnedProcess(_ExitedOwnedProcess):
    def __init__(self) -> None:
        super().__init__()
        self.alive = True
        self.closed = False

    def is_alive(self) -> bool:
        if self.closed:
            raise ValueError("process object is closed")
        return self.alive

    def close(self) -> None:
        super().close()
        self.closed = True


def test_reaper_does_not_close_a_process_still_owned_by_run_process(
    tmp_path: Path,
) -> None:
    materializer = ImageMediaMaterializer(
        root=tmp_path / "media",
        download=None,  # type: ignore[arg-type]
    )
    process = _ExitedOwnedProcess()
    materializer._active_processes.add(process)  # type: ignore[arg-type]

    materializer._reap_processes()

    assert process in materializer._active_processes
    assert process not in materializer._retained_processes
    assert process.join_calls == 0
    assert process.close_calls == 0

    # Once an owning coroutine has explicitly retained the handle after failed
    # termination, a later call may safely reap it.
    materializer._retained_processes.add(process)  # type: ignore[arg-type]
    materializer._reap_processes()
    assert process not in materializer._active_processes
    assert process not in materializer._retained_processes
    assert process.join_calls == 1
    assert process.close_calls == 1


def test_owner_finalizer_clears_a_retained_process_that_exits_late(
    tmp_path: Path,
) -> None:
    materializer = ImageMediaMaterializer(
        root=tmp_path / "media",
        download=None,  # type: ignore[arg-type]
    )
    process = _ExitedOwnedProcess()
    materializer._active_processes.add(process)  # type: ignore[arg-type]
    materializer._retained_processes.add(process)  # type: ignore[arg-type]

    assert materializer._close_finished_owned_process(process) is True
    assert process not in materializer._active_processes
    assert process not in materializer._retained_processes
    assert process.join_calls == 1
    assert process.close_calls == 1

    # A later request can safely run the retained-worker reaper.
    materializer._reap_processes()


@pytest.mark.asyncio
async def test_termination_does_not_close_a_completed_worker_still_owned_by_run(
    tmp_path: Path,
) -> None:
    materializer = ImageMediaMaterializer(
        root=tmp_path / "media",
        download=None,  # type: ignore[arg-type]
    )
    process = _ExitedOwnedProcess()
    materializer._active_processes.add(process)  # type: ignore[arg-type]

    await materializer._terminate_owned_process(process)

    assert process not in materializer._active_processes
    assert process not in materializer._retained_processes
    assert process.join_calls == 1
    assert process.close_calls == 0

    assert materializer._close_finished_owned_process(process) is True
    assert process.join_calls == 2
    assert process.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_from_terminate", [False, True])
async def test_shutdown_accepts_owner_closing_process_during_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_from_terminate: bool,
) -> None:
    materializer = ImageMediaMaterializer(
        root=tmp_path / "media",
        download=None,  # type: ignore[arg-type]
    )
    process = _ClosingOwnedProcess()
    materializer._active_processes.add(process)  # type: ignore[arg-type]

    async def owner_closes_during_termination(_process) -> None:
        process.alive = False
        assert materializer._close_finished_owned_process(process) is True
        if raise_from_terminate:
            raise ValueError("process object is closed")

    monkeypatch.setattr(
        "imcodex.channels.media._terminate_process",
        owner_closes_during_termination,
    )

    await materializer._terminate_owned_process(process)

    assert process not in materializer._active_processes
    assert process not in materializer._retained_processes
    assert process.closed is True
    assert materializer._tainted is False


@pytest.mark.asyncio
async def test_unkillable_stage_worker_is_retained_and_materializer_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.media.MEDIA_PROCESS_TERMINATE_S", 0.0)
    process = _UnkillableProcess()

    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(
            root=tmp_path / "media",
            http_client=client,
        )
        materializer._active_processes.add(process)  # type: ignore[arg-type]
        with pytest.raises(MediaSpoolError):
            await materializer._terminate_owned_process(process)

        result = await materializer.materialize((_reference(),))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process in materializer._active_processes
    assert process in materializer._retained_processes
    assert materializer._tainted is True
    assert result == QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)


@pytest.mark.asyncio
async def test_failed_transaction_cleanup_stays_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(
            root=tmp_path / "media",
            http_client=client,
        )

        async def failed_cleanup_process(**_kwargs):
            return ("error", ())

        monkeypatch.setattr(materializer, "_run_process", failed_cleanup_process)
        with pytest.raises(MediaSpoolError):
            await materializer._cleanup_known_paths((tmp_path / "orphan.png",))
        materializer._reap_processes()

    assert materializer._tainted is True


@pytest.mark.asyncio
async def test_materializer_fails_closed_when_spool_entry_bound_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imcodex.channels.media.MEDIA_MAX_SPOOL_ENTRIES", 2)
    root = tmp_path / "media"
    root.mkdir()
    for index in range(3):
        (root / f"entry-{index}.part").write_bytes(b"")
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QQMediaMaterializer(
            root=root,
            http_client=client,
        ).materialize((_reference(),))

    assert result == QQMediaResult(input_error=IMAGE_DOWNLOAD_FAILED)
    assert requests == 0


@pytest.mark.asyncio
async def test_materializer_prepare_hides_filesystem_paths(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    root = blocked_parent / "inbound-media"

    async with httpx.AsyncClient() as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        with pytest.raises(MediaSpoolError) as captured:
            await materializer.prepare()

    assert str(captured.value) == MEDIA_SPOOL_UNAVAILABLE_MESSAGE
    assert str(root) not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_materializer_removes_only_expired_files_and_honors_quota(tmp_path: Path) -> None:
    now = 2_000_000.0
    root = tmp_path / "media"
    root.mkdir()
    old = root / "old.png"
    old.write_bytes(PNG)
    os.utime(old, (now - MEDIA_RETENTION_S - 1, now - MEDIA_RETENTION_S - 1))
    fresh_part = root / "other-process.part"
    fresh_part.write_bytes(b"active")
    os.utime(fresh_part, (now, now))

    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=root, http_client=client, clock=lambda: now)
        await materializer.prepare()
        assert not old.exists()
        assert fresh_part.exists()
        quota_file = root / "quota.bin"
        with quota_file.open("wb") as stream:
            stream.truncate(MEDIA_QUOTA_BYTES)
        result = await materializer.materialize((_reference(),))

    assert result.input_error == IMAGE_DOWNLOAD_FAILED
    assert requests == 0
    assert fresh_part.exists()
    assert quota_file.exists()


@pytest.mark.asyncio
async def test_materializer_serializes_concurrent_quota_spending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota = len(PNG)
    monkeypatch.setattr("imcodex.channels.media.MEDIA_QUOTA_BYTES", quota)
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        return httpx.Response(200, content=PNG)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        results = await asyncio.gather(
            materializer.materialize((_reference(),)),
            materializer.materialize((_reference(),)),
        )

    assert sum(result.input_error is None for result in results) == 1
    assert sum(result.input_error == IMAGE_DOWNLOAD_FAILED for result in results) == 1
    assert requests == 1
    assert sum(path.stat().st_size for path in root.iterdir()) <= quota


@pytest.mark.asyncio
async def test_normal_concurrent_workers_do_not_taint_later_image_work(
    tmp_path: Path,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        return httpx.Response(200, content=PNG)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materializer = QQMediaMaterializer(root=root, http_client=client)
        concurrent = await asyncio.gather(
            materializer.materialize((_reference(),)),
            materializer.materialize((_reference(),)),
        )
        later = await materializer.materialize((_reference(),))

    assert all(result.input_error is None for result in (*concurrent, later))
    assert requests == 3
    assert materializer._tainted is False


@pytest.mark.asyncio
async def test_materializer_serializes_quota_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota = len(PNG)
    monkeypatch.setattr("imcodex.channels.media.MEDIA_QUOTA_BYTES", quota)

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(200, content=PNG)

    root = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = QQMediaMaterializer(root=root, http_client=client)
        second = QQMediaMaterializer(root=root, http_client=client)
        results = await asyncio.gather(
            first.materialize((_reference(),)),
            second.materialize((_reference(),)),
        )

    assert sum(result.input_error is None for result in results) == 1
    assert sum(path.stat().st_size for path in root.iterdir()) <= quota


def test_materializer_serializes_quota_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "shared-media"
    barrier = tmp_path / "start"
    worker = tmp_path / "quota_worker.py"
    worker.write_text(
        f"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import imcodex.channels.media as media


BODY = {PNG!r}


async def download(_reference, write_chunk) -> None:
    await asyncio.sleep(0.05)
    await write_chunk(BODY)


async def main() -> None:
    root = Path(sys.argv[1])
    barrier = Path(sys.argv[2])
    while not barrier.exists():
        await asyncio.sleep(0.01)
    media.MEDIA_QUOTA_BYTES = len(BODY)
    materializer = media.ImageMediaMaterializer(root=root, download=download)
    result = await materializer.materialize(("image",))
    print(result.input_error or "ok", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
""".lstrip(),
        encoding="utf-8",
    )
    processes = [
        subprocess.Popen(
            [sys.executable, str(worker), str(root), str(barrier)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        barrier.touch()
        outputs: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert sorted(outputs) == [IMAGE_DOWNLOAD_FAILED, "ok"]
    staged = list(root.iterdir())
    assert len(staged) == 1
    assert staged[0].stat().st_size == len(PNG)


@pytest.mark.asyncio
async def test_materializer_lock_is_released_when_owner_process_terminates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "shared-media"
    marker = tmp_path / "locked"
    worker = tmp_path / "lock_owner.py"
    worker.write_text(
        """
from pathlib import Path
import sys
import time

from imcodex.channels.media import ImageMediaMaterializer


root = Path(sys.argv[1])
marker = Path(sys.argv[2])
materializer = ImageMediaMaterializer(root=root, download=None)
status, descriptor = materializer._try_process_lock(True)
if status != "acquired" or descriptor is None:
    raise RuntimeError("failed to acquire media lock")
marker.write_text("locked", encoding="utf-8")
time.sleep(60)
""".lstrip(),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(worker), str(root), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(500):
            if marker.exists() or process.poll() is not None:
                break
            await asyncio.sleep(0.01)
        error = (
            process.stderr.read()
            if process.poll() is not None and process.stderr is not None
            else "lock owner did not become ready"
        )
        assert marker.exists(), error
        process.terminate()
        await asyncio.to_thread(process.wait, 5)

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PNG)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await QQMediaMaterializer(
                root=root,
                http_client=client,
            ).materialize((_reference(),))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert result.input_error is None


def test_qq_parser_accepts_pure_image_and_group_mention_only_image(tmp_path: Path) -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        media_dir=tmp_path,
    )
    attachment = {"content_type": "image/png", "url": TRUSTED_URL}

    direct = adapter.parse_inbound_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "direct-image",
            "content": "",
            "author": {"user_openid": "user-1"},
            "attachments": [attachment],
        },
    )
    group = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "group-image",
            "content": "<@123>",
            "author": {"member_openid": "user-1"},
            "group_openid": "group-1",
            "attachments": [attachment],
        },
    )

    assert direct is not None and direct.text == ""
    assert direct.conversation_id == "c2c:user-1"
    assert group is not None and group.text == ""
    assert group.conversation_id == "group:group-1"


class _FakeMaterializer:
    def __init__(self, result: QQMediaResult) -> None:
        self.result = result
        self.calls = 0

    async def prepare(self) -> None:
        pass

    async def start(self) -> None:
        await self.prepare()

    async def stop(self) -> None:
        pass

    async def materialize(self, _references) -> QQMediaResult:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_qq_starts_and_stops_shared_media_lifecycle(tmp_path: Path) -> None:
    class LifecycleMaterializer(_FakeMaterializer):
        def __init__(self) -> None:
            super().__init__(QQMediaResult())
            self.start_calls = 0
            self.stop_calls = 0

        async def start(self) -> None:
            self.start_calls += 1

        async def stop(self) -> None:
            self.stop_calls += 1

    materializer = LifecycleMaterializer()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        media_dir=tmp_path,
        media_materializer=materializer,  # type: ignore[arg-type]
    )
    adapter._run_forever = lambda: asyncio.Event().wait()  # type: ignore[method-assign]

    await adapter.start()
    await adapter.stop()

    assert materializer.start_calls == 1
    assert materializer.stop_calls == 1


def test_qq_access_policy_runs_before_media_materialization(tmp_path: Path) -> None:
    materializer = _FakeMaterializer(QQMediaResult())
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        media_dir=tmp_path,
        media_materializer=materializer,  # type: ignore[arg-type]
        access_policy=ChannelAccessPolicy(allowed_user_ids=frozenset({"owner"})),
    )

    adapter._queue_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "blocked-image",
            "content": "",
            "author": {"user_openid": "intruder"},
            "attachments": [{"content_type": "image/png", "url": TRUSTED_URL}],
        },
        7,
    )

    assert materializer.calls == 0
    assert adapter._inbound_queue.empty()
    assert adapter._inbound_worker_task is None


@pytest.mark.asyncio
async def test_qq_worker_materializes_once_across_delivery_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FlakyMiddleware:
        def __init__(self) -> None:
            self.calls = 0
            self.seen = []

        async def handle_inbound(
            self,
            _adapter,
            inbound,
            *,
            reply_to_message_id=None,
            prepare_inbound=None,
            pending_attachment_count=0,
        ):
            if not inbound.attachments and prepare_inbound is not None:
                inbound = await prepare_inbound(inbound)
            self.calls += 1
            self.seen.append(inbound)
            if self.calls == 1:
                raise OSError("temporary delivery failure")

    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()

    async def controlled_sleep(_delay: float) -> None:
        retry_waiting.set()
        await release_retry.wait()

    local_path = str((tmp_path / "image.png").absolute())
    observed_events: list[dict] = []
    monkeypatch.setattr(
        "imcodex.channels.media.emit_event",
        lambda **payload: observed_events.append(payload),
    )
    materializer = _FakeMaterializer(
        QQMediaResult(
            images=(
                QQMaterializedImage(
                    content_type="image/png",
                    local_path=local_path,
                    size_bytes=len(PNG),
                ),
            )
        )
    )
    middleware = FlakyMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        media_dir=tmp_path,
        media_materializer=materializer,  # type: ignore[arg-type]
        sleep=controlled_sleep,
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    adapter._queue_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "image-1",
            "content": "describe this",
            "author": {"user_openid": "user-1"},
            "attachments": [{"content_type": "image/png", "url": TRUSTED_URL}],
        },
        9,
    )

    await retry_waiting.wait()
    assert materializer.calls == 1
    release_retry.set()
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert materializer.calls == 1
    assert middleware.calls == 2
    assert middleware.seen[0] is middleware.seen[1]
    assert middleware.seen[0].attachments[0].kind == "image"
    assert adapter._last_seq == 9
    media_events = [
        event
        for event in observed_events
        if event["event"].startswith("message.inbound.media.")
    ]
    assert [event["event"] for event in media_events] == [
        "message.inbound.media.materializing",
        "message.inbound.media.materialized",
    ]
    assert local_path not in repr(media_events)
    await adapter.stop()


@pytest.mark.asyncio
async def test_qq_gateway_replay_is_deduplicated_before_media_materialization(
    tmp_path: Path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware
    from imcodex.store import ConversationStore

    class Service:
        def __init__(self) -> None:
            self.store = ConversationStore(
                state_path=tmp_path / "state.json",
                clock=lambda: 1.0,
            )
            self.calls = 0

        async def handle_inbound(self, _inbound):
            self.calls += 1
            return []

    materializer = _FakeMaterializer(
        QQMediaResult(
            images=(
                QQMaterializedImage(
                    content_type="image/png",
                    local_path=str((tmp_path / "staged.png").absolute()),
                    size_bytes=len(PNG),
                ),
            )
        )
    )
    service = Service()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=UnifiedChannelMiddleware(service=service),
        media_dir=tmp_path,
        media_materializer=materializer,  # type: ignore[arg-type]
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    payload = {
        "id": "image-replay-1",
        "content": "describe this",
        "author": {"user_openid": "user-1"},
        "attachments": [{"content_type": "image/png", "url": TRUSTED_URL}],
    }

    adapter._queue_dispatch_event("C2C_MESSAGE_CREATE", payload, 20)
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)
    adapter._queue_dispatch_event("C2C_MESSAGE_CREATE", payload, 20)
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert materializer.calls == 1
    assert service.calls == 1
    assert adapter._last_seq == 20
    await adapter.stop()


@pytest.mark.asyncio
async def test_qq_worker_dispatches_stable_media_error_and_advances_sequence(tmp_path: Path) -> None:
    class CapturingMiddleware:
        def __init__(self) -> None:
            self.seen = []

        async def handle_inbound(
            self,
            _adapter,
            inbound,
            *,
            reply_to_message_id=None,
            prepare_inbound=None,
            pending_attachment_count=0,
        ):
            if prepare_inbound is not None:
                inbound = await prepare_inbound(inbound)
            self.seen.append(inbound)

    materializer = _FakeMaterializer(QQMediaResult(input_error=UNSUPPORTED_IMAGE))
    middleware = CapturingMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        media_dir=tmp_path,
        media_materializer=materializer,  # type: ignore[arg-type]
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    adapter._queue_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "bad-image",
            "content": "",
            "author": {"user_openid": "user-1"},
            "attachments": [{"content_type": "image/gif", "url": TRUSTED_URL}],
        },
        11,
    )

    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert materializer.calls == 1
    assert middleware.seen[0].input_error == UNSUPPORTED_IMAGE
    assert middleware.seen[0].attachments == ()
    assert adapter._last_seq == 11
    await adapter.stop()

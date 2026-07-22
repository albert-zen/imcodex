from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from imcodex.channels.media import (
    FILE_DOWNLOAD_FAILED,
    FILE_TOO_LARGE,
    INVALID_FILE,
    TOO_MANY_FILES,
    UNSUPPORTED_FILE,
    FileMediaMaterializer,
    MAX_FILE_COUNT,
    _release_process_lock,
    _try_process_lock_path,
)


@dataclass(frozen=True, slots=True)
class FileReference:
    filename: str
    content_type: str
    content: bytes


async def _download(reference: FileReference, write_chunk) -> None:
    await write_chunk(reference.content)


VALID_PDF = (
    b"%PDF-1.7\n"
    b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"xref\n0 1\n0000000000 65535 f \n"
    b"trailer\n<< /Root 1 0 R /Size 1 >>\n"
    b"startxref\n42\n%%EOF\n"
)


@pytest.mark.asyncio
async def test_file_materializer_stages_pdf_with_bounded_untrusted_filename(
    tmp_path: Path,
) -> None:
    materializer = FileMediaMaterializer(root=tmp_path / "media", download=_download)

    result = await materializer.materialize(
        (
            FileReference(
                filename="../../quarterly-report.pdf",
                content_type="application/octet-stream",
                content=VALID_PDF,
            ),
        )
    )

    assert result.input_error is None
    assert result.files[0].filename == "quarterly-report.pdf"
    assert result.files[0].content_type == "application/pdf"
    staged = Path(result.files[0].local_path)
    assert staged.parent == (tmp_path / "media")
    assert staged.read_bytes() == VALID_PDF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (FileReference("archive.zip", "application/zip", b"PK\x03\x04"), UNSUPPORTED_FILE),
        (FileReference("notes.md", "text/markdown", b"bad\x00text"), INVALID_FILE),
        (FileReference("broken.pdf", "application/pdf", b"not pdf"), INVALID_FILE),
        (FileReference("truncated.pdf", "application/pdf", b"%PDF-1.7\nbody"), INVALID_FILE),
    ],
)
async def test_file_materializer_rejects_unsupported_or_inconsistent_content(
    tmp_path: Path,
    reference: FileReference,
    expected: str,
) -> None:
    materializer = FileMediaMaterializer(root=tmp_path / "media", download=_download)

    result = await materializer.materialize((reference,))

    assert result.files == ()
    assert result.input_error == expected


@pytest.mark.asyncio
async def test_file_materializer_enforces_count_and_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    materializer = FileMediaMaterializer(root=tmp_path / "media", download=_download)
    reference = FileReference("notes.txt", "text/plain", b"hello")

    too_many = await materializer.materialize(
        tuple(replace(reference, filename=f"{index}.txt") for index in range(MAX_FILE_COUNT + 1))
    )
    monkeypatch.setattr("imcodex.channels.media.MAX_FILE_BYTES", 4)
    too_large = await materializer.materialize((reference,))

    assert too_many.input_error == TOO_MANY_FILES
    assert too_large.input_error == FILE_TOO_LARGE


@pytest.mark.asyncio
async def test_file_materializer_timeout_terminates_staging_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "media"
    lock_status, descriptor = _try_process_lock_path(root, create=True)
    assert lock_status == "acquired"
    assert descriptor is not None
    monkeypatch.setattr("imcodex.channels.media.MEDIA_MATERIALIZE_DEADLINE_S", 0.05)
    materializer = FileMediaMaterializer(root=root, download=_download)
    try:
        result = await materializer.materialize(
            (FileReference("notes.txt", "text/plain", b"hello"),)
        )
    finally:
        _release_process_lock(descriptor)

    assert result.input_error == FILE_DOWNLOAD_FAILED
    assert not materializer._active_processes
    assert not tuple(root.glob("*.txt"))

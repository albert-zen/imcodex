from __future__ import annotations

import pytest

from imcodex.delivery_artifacts import DeliveryArtifactStager


def test_delivery_artifact_stager_hashes_and_reuses_upload(tmp_path) -> None:
    stager = DeliveryArtifactStager(tmp_path / "spool")
    first = stager.stage_upload(
        b"plain text",
        kind="file",
        content_type="text/plain",
        filename="result.txt",
    )
    second = stager.stage_upload(
        b"plain text",
        kind="file",
        content_type="text/plain",
        filename="other.txt",
    )
    assert first.sha256 == second.sha256
    assert first.local_path == second.local_path


def test_delivery_artifact_stager_rejects_invalid_image(tmp_path) -> None:
    stager = DeliveryArtifactStager(tmp_path / "spool")
    with pytest.raises(ValueError, match="valid image"):
        stager.stage_upload(
            b"not an image",
            kind="image",
            content_type="image/png",
            filename="image.png",
        )

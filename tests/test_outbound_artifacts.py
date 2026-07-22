from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
import os

import pytest
from PIL import Image

from imcodex.bridge.outbound_artifacts import OutboundArtifactStager


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), (4, 5, 6)).save(stream, format="PNG")
    return stream.getvalue()


def test_stager_materializes_dynamic_tool_image_data_url(tmp_path: Path) -> None:
    content = _png()
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_native_item(
        {
            "type": "dynamicToolCall",
            "contentItems": [
                {
                    "type": "inputImage",
                    "imageUrl": "data:image/png;base64," + base64.b64encode(content).decode("ascii"),
                }
            ],
        }
    )

    assert len(artifacts) == 1
    assert Path(artifacts[0].local_path).read_bytes() == content
    assert artifacts[0].content_type == "image/png"
    assert artifacts[0].sha256 == hashlib.sha256(content).hexdigest()


def test_stager_rejects_markdown_file_outside_native_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    stager = OutboundArtifactStager(tmp_path / "spool")

    with pytest.raises(ValueError, match="outside the native workspace"):
        stager.stage_markdown_links(f"[secret]({outside})", cwd=str(workspace))


def test_stager_refuses_preexisting_content_address_collision(tmp_path: Path) -> None:
    content = _png()
    digest = hashlib.sha256(content).hexdigest()
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / f"{digest}.png").write_bytes(b"not the staged image")
    stager = OutboundArtifactStager(spool)

    with pytest.raises(ValueError, match="does not match its digest"):
        stager.stage_native_item(
            {
                "type": "dynamicToolCall",
                "contentItems": [
                    {
                        "type": "inputImage",
                        "imageUrl": "data:image/png;base64,"
                        + base64.b64encode(content).decode("ascii"),
                    }
                ],
            }
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows file URL conversion")
def test_stager_accepts_windows_file_url_inside_native_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "generated.png"
    image_path.write_bytes(_png())
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_native_item(
        {
            "type": "dynamicToolCall",
            "contentItems": [
                {"type": "inputImage", "imageUrl": image_path.as_uri()}
            ],
        },
        cwd=str(workspace),
    )

    assert len(artifacts) == 1
    assert Path(artifacts[0].local_path).read_bytes() == _png()


def test_stager_startup_cleanup_preserves_durable_references(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    preserved = spool / "preserved.png"
    stale = spool / "stale.png"
    preserved.write_bytes(_png())
    stale.write_bytes(_png())
    stager = OutboundArtifactStager(spool)

    stager.cleanup_unreferenced({str(preserved)})

    assert preserved.exists()
    assert not stale.exists()

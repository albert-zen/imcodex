from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
import os
import threading

import pytest
from PIL import Image

from imcodex.bridge.outbound_artifacts import OutboundArtifactStager
from imcodex.models import OutboundArtifact


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


def test_stager_ignores_ordinary_markdown_file_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_markdown_images(
        f"[secret]({outside})",
        cwd=str(workspace),
    )

    assert artifacts == ()


def test_stager_ignores_ordinary_markdown_links_to_images(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "preview.png"
    image_path.write_bytes(_png())
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_markdown_images(
        f"[preview]({image_path})",
        cwd=str(workspace),
    )

    assert artifacts == ()


def test_stager_recognizes_jfif_without_host_mime_database(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "preview.jfif"
    Image.new("RGB", (2, 2), (4, 5, 6)).save(image_path, format="JPEG")
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_markdown_images(
        f"![preview]({image_path})",
        cwd=str(workspace),
    )

    assert len(artifacts) == 1
    assert artifacts[0].kind == "image"
    assert artifacts[0].content_type == "image/jpeg"


def test_stager_ignores_host_mime_image_false_positives(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drawing_path = workspace / "drawing.dwg"
    drawing_path.write_bytes(b"not a raster image")
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_markdown_images(
        f"[drawing]({drawing_path})",
        cwd=str(workspace),
    )

    assert artifacts == ()


def test_stager_accepts_explicit_markdown_image_outside_native_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(_png())
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_markdown_images(
        f"![preview]({outside})",
        cwd=str(workspace),
    )

    assert len(artifacts) == 1
    assert artifacts[0].filename == "secret.png"
    assert Path(artifacts[0].local_path).read_bytes() == _png()


@pytest.mark.parametrize(
    "text",
    [
        "```markdown\n![example](/Users/xxx/secret.png)\n```",
        "~~~\n![example](/Users/xxx/secret.png)\n~~~",
        "Use `![example](/Users/xxx/secret.png)` to embed an image.",
        r"\![example](/Users/xxx/secret.png)",
    ],
)
def test_stager_ignores_markdown_image_syntax_that_is_not_an_image_node(
    tmp_path: Path,
    text: str,
) -> None:
    stager = OutboundArtifactStager(tmp_path / "spool")

    assert stager.stage_markdown_images(text, cwd=str(tmp_path)) == ()


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


def test_cleanup_cannot_delete_artifact_while_stage_result_is_being_handed_off(
    tmp_path: Path,
) -> None:
    stager = OutboundArtifactStager(tmp_path / "spool")
    staged = threading.Event()
    release_stage = threading.Event()
    original = stager._stage_bytes
    result: list[OutboundArtifact] = []

    def paused_stage_bytes(*args, **kwargs):
        artifact = original(*args, **kwargs)
        staged.set()
        assert release_stage.wait(timeout=5)
        return artifact

    stager._stage_bytes = paused_stage_bytes  # type: ignore[method-assign]
    stage_thread = threading.Thread(
        target=lambda: result.append(
            stager.stage_upload(
                b"# durable\n",
                kind="file",
                content_type="text/markdown",
                filename="durable.md",
            )
        )
    )
    cleanup_thread = threading.Thread(
        target=lambda: stager.cleanup_unreferenced(set())
    )

    stage_thread.start()
    assert staged.wait(timeout=5)
    cleanup_thread.start()
    assert cleanup_thread.is_alive()
    release_stage.set()
    stage_thread.join(timeout=5)
    cleanup_thread.join(timeout=5)

    assert not stage_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert len(result) == 1
    assert Path(result[0].local_path).read_bytes() == b"# durable\n"

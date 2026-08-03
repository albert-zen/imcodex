from __future__ import annotations

import base64
import hashlib
import os
import threading
from io import BytesIO
from pathlib import Path

import pytest
from imagent.applications import AppServerArtifactCandidate, AppServerArtifactSourceKind
from PIL import Image

from imcodex.bridge.outbound_artifacts import (
    OutboundArtifactLeaseLedger,
    OutboundArtifactStager,
)
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
                    "imageUrl": "data:image/png;base64,"
                    + base64.b64encode(content).decode("ascii"),
                }
            ],
        }
    )

    assert len(artifacts) == 1
    assert Path(artifacts[0].local_path).read_bytes() == content
    assert artifacts[0].content_type == "image/png"
    assert artifacts[0].sha256 == hashlib.sha256(content).hexdigest()


def test_stager_materializes_typed_sdk_artifact_candidate(tmp_path: Path) -> None:
    content = _png()
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifact = stager.stage_appserver_candidate(
        AppServerArtifactCandidate(
            candidate_id="tool-1:image:0",
            source_kind=AppServerArtifactSourceKind.DATA_URL,
            locator="data:image/png;base64,"
            + base64.b64encode(content).decode("ascii"),
        )
    )

    assert Path(artifact.local_path).read_bytes() == content
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()


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
def test_stager_accepts_windows_file_url_inside_native_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "generated.png"
    image_path.write_bytes(_png())
    stager = OutboundArtifactStager(tmp_path / "spool")

    artifacts = stager.stage_native_item(
        {
            "type": "dynamicToolCall",
            "contentItems": [{"type": "inputImage", "imageUrl": image_path.as_uri()}],
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
    cleanup_thread = threading.Thread(target=lambda: stager.cleanup_unreferenced(set()))

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


def test_lease_ledger_preserves_transferred_artifact_across_restart(
    tmp_path: Path,
) -> None:
    class ProductStore:
        @staticmethod
        def referenced_terminal_artifact_paths() -> set[str]:
            return set()

    spool = tmp_path / "spool"
    state_path = tmp_path / "leases.json"
    first_stager = OutboundArtifactStager(spool)
    artifact = first_stager.stage_upload(
        b"# durable\n",
        kind="file",
        content_type="text/markdown",
        filename="durable.md",
    )
    first = OutboundArtifactLeaseLedger(
        stager=first_stager,
        product_store=ProductStore(),
        state_path=state_path,
    )
    first.transfer("delivery-1", (artifact,))

    restarted = OutboundArtifactLeaseLedger(
        stager=OutboundArtifactStager(spool),
        product_store=ProductStore(),
        state_path=state_path,
    )
    restarted.cleanup()

    assert Path(artifact.local_path).exists()
    assert restarted.referenced_paths() == {artifact.local_path}

    restarted.complete_delivery("delivery-1", (artifact.local_path,))

    assert not Path(artifact.local_path).exists()
    assert restarted.referenced_paths() == set()


def test_lease_ledger_replay_requires_same_artifacts(tmp_path: Path) -> None:
    class ProductStore:
        @staticmethod
        def referenced_terminal_artifact_paths() -> set[str]:
            return set()

    stager = OutboundArtifactStager(tmp_path / "spool")
    first = stager.stage_upload(
        b"first\n",
        kind="file",
        content_type="text/plain",
        filename="first.txt",
    )
    second = stager.stage_upload(
        b"second\n",
        kind="file",
        content_type="text/plain",
        filename="second.txt",
    )
    ledger = OutboundArtifactLeaseLedger(
        stager=stager,
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    ledger.transfer("delivery-1", (first,))
    ledger.transfer("delivery-1", (first,))

    with pytest.raises(ValueError, match="different artifacts"):
        ledger.transfer("delivery-1", (second,))


def test_same_upload_content_reuses_path_for_delivery_replay(tmp_path: Path) -> None:
    stager = OutboundArtifactStager(tmp_path / "spool")

    first = stager.stage_upload(
        b"same\n", kind="file", content_type="text/plain", filename="first.txt"
    )
    replay = stager.stage_upload(
        b"same\n", kind="file", content_type="text/plain", filename="renamed.txt"
    )

    assert replay.local_path == first.local_path


@pytest.mark.asyncio
async def test_lease_ledger_reconciles_terminal_sdk_submission(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from imagent.contracts import DeliverySubmissionState

    class ProductStore:
        @staticmethod
        def referenced_terminal_artifact_paths() -> set[str]:
            return set()

    stager = OutboundArtifactStager(tmp_path / "spool")
    artifact = stager.stage_upload(
        b"terminal\n",
        kind="file",
        content_type="text/plain",
        filename="terminal.txt",
    )
    ledger = OutboundArtifactLeaseLedger(
        stager=stager,
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    ledger.transfer("delivery-1", (artifact,))

    class Submissions:
        async def get_delivery_submission(self, _submission_id):
            return SimpleNamespace(
                destinations=(SimpleNamespace(state=DeliverySubmissionState.ACCEPTED),)
            )

    await ledger.reconcile(Submissions())

    assert ledger.referenced_paths() == set()
    assert not Path(artifact.local_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "in_flight",
        "retryable",
        "partial",
    ],
)
async def test_lease_ledger_preserves_nonterminal_or_partial_submission(
    tmp_path: Path,
    state: str,
) -> None:
    from types import SimpleNamespace

    from imagent.contracts import DeliverySubmissionState

    class ProductStore:
        @staticmethod
        def referenced_terminal_artifact_paths() -> set[str]:
            return set()

    stager = OutboundArtifactStager(tmp_path / "spool")
    artifact = stager.stage_upload(
        state.encode(),
        kind="file",
        content_type="text/plain",
        filename=f"{state}.txt",
    )
    ledger = OutboundArtifactLeaseLedger(
        stager=stager,
        product_store=ProductStore(),
        state_path=tmp_path / "leases.json",
    )
    ledger.transfer("delivery-1", (artifact,))

    class Submissions:
        async def get_delivery_submission(self, _submission_id):
            return SimpleNamespace(
                destinations=(SimpleNamespace(state=DeliverySubmissionState(state)),)
            )

    await ledger.reconcile(Submissions())

    assert ledger.referenced_paths() == {artifact.local_path}
    assert Path(artifact.local_path).exists()

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from threading import RLock

from PIL import Image

from .models import OutboundArtifact


_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_SPOOL_BYTES = 256 * 1024 * 1024


class DeliveryArtifactStager:
    """Stage product-owned upload bytes for public SDK delivery."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = RLock()
        self._leased_paths: set[str] = set()

    def stage_upload(
        self,
        content: bytes,
        *,
        kind: str,
        content_type: str,
        filename: str,
    ) -> OutboundArtifact:
        with self._lock:
            safe_name = Path(str(filename or "").replace("\\", "/")).name
            if not safe_name or len(safe_name) > 120:
                raise ValueError("artifact filename must be between 1 and 120 characters")
            if kind not in {"image", "file"}:
                raise ValueError("artifact kind must be image or file")
            limit = _MAX_IMAGE_BYTES if kind == "image" else _MAX_FILE_BYTES
            if len(content) > limit:
                raise ValueError(f"{kind} output exceeds the delivery size limit")
            if kind == "file":
                from .file_types import detect_generic_file

                content_type, _suffix = detect_generic_file(safe_name, content)
            return self._stage_bytes(
                content,
                kind=kind,
                content_type=content_type or "application/octet-stream",
                filename=safe_name,
            )

    def release(self, artifacts) -> None:
        with self._lock:
            for artifact in artifacts:
                path = str(getattr(artifact, "local_path", artifact) or "")
                if path:
                    self._leased_paths.discard(path)

    def cleanup_unreferenced(self, referenced_paths: set[str]) -> None:
        with self._lock:
            if not self.root.exists():
                return
            root = self.root.resolve(strict=False)
            referenced: set[Path] = set()
            for value in {*referenced_paths, *self._leased_paths}:
                try:
                    candidate = Path(value).resolve(strict=False)
                    candidate.relative_to(root)
                except (OSError, ValueError):
                    continue
                referenced.add(candidate)
            for candidate in self.root.iterdir():
                try:
                    resolved = candidate.resolve(strict=False)
                    if candidate.is_file() and resolved not in referenced:
                        candidate.unlink(missing_ok=True)
                except OSError:
                    continue

    def _stage_bytes(
        self,
        content: bytes,
        *,
        kind: str,
        content_type: str,
        filename: str,
    ) -> OutboundArtifact:
        if kind == "image":
            try:
                from io import BytesIO

                with Image.open(BytesIO(content)) as image:
                    image.verify()
                    detected = Image.MIME.get(image.format or "")
            except Exception as exc:
                raise ValueError("output artifact is not a valid image") from exc
            if detected:
                content_type = detected

        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(filename).suffix.lower()
        if not suffix:
            suffix = mimetypes.guess_extension(content_type) or ".bin"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)
        target = self.root / f"{digest}{suffix}"
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != digest
            ):
                raise ValueError("managed outbound artifact content does not match its digest")
        else:
            self._ensure_spool_capacity(len(content))
            temporary = self.root / f".{digest}.{os.getpid()}.tmp"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if os.name != "nt":
            os.chmod(target, 0o600)
        artifact = OutboundArtifact(
            kind=kind,
            local_path=str(target.resolve()),
            content_type=content_type,
            filename=Path(filename).name or target.name,
            size_bytes=len(content),
            sha256=digest,
        )
        self._leased_paths.add(artifact.local_path)
        return artifact

    def _ensure_spool_capacity(self, incoming_bytes: int) -> None:
        total = sum(
            candidate.stat().st_size
            for candidate in self.root.iterdir()
            if candidate.is_file()
        )
        if total + incoming_bytes > _MAX_SPOOL_BYTES:
            raise ValueError("outbound artifact spool exceeds the 256 MiB limit")

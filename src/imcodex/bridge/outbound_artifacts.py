from __future__ import annotations

import base64
import binascii
from functools import wraps
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import secrets
from threading import RLock
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from PIL import Image
from imagent.applications import AppServerArtifactCandidate, AppServerArtifactSourceKind

from ..models import OutboundArtifact


_MARKDOWN_LINK = re.compile(r"(!?)\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+['\"][^'\"]*['\"])?\)")
_DATA_IMAGE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_SPOOL_BYTES = 256 * 1024 * 1024


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def _serialized_stage(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            previous_leases = set(self._leased_paths)
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                self._leased_paths.intersection_update(previous_leases)
                raise

    return wrapped


class OutboundArtifactStager:
    """Copies explicit native outputs into a private, content-addressed spool."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = RLock()
        self._leased_paths: set[str] = set()

    @_serialized_stage
    def stage_appserver_candidate(
        self,
        candidate: AppServerArtifactCandidate,
    ) -> OutboundArtifact:
        """Validate and stage one SDK-normalized, still-untrusted native candidate."""

        if candidate.media_kind != "image":
            raise ValueError("unsupported App Server artifact media kind")
        if candidate.source_kind is AppServerArtifactSourceKind.LOCAL_PATH:
            return self._stage_local(candidate.value, kind="image")
        if candidate.source_kind is AppServerArtifactSourceKind.FILE_URL:
            parsed = urlparse(candidate.value)
            if parsed.scheme != "file":
                raise ValueError("App Server file candidate must use a file URL")
            return self._stage_local(self._file_url_path(parsed), kind="image")
        if candidate.source_kind is AppServerArtifactSourceKind.DATA_URL:
            return self._stage_data_image(candidate.value, filename_hint="image")
        raise ValueError("unsupported App Server artifact source kind")

    @_serialized_stage
    def stage_native_item(self, item: dict, *, cwd: str = "") -> tuple[OutboundArtifact, ...]:
        item_type = str(item.get("type") or "")
        if item_type == "imageGeneration":
            saved_path = str(item.get("savedPath") or "").strip()
            if saved_path:
                return (
                    self._stage_local(
                        saved_path,
                        kind="image",
                    ),
                )
            return ()
        if item_type != "dynamicToolCall":
            return ()
        artifacts: list[OutboundArtifact] = []
        for index, content in enumerate(item.get("contentItems") or []):
            if not isinstance(content, dict) or content.get("type") != "inputImage":
                continue
            image_url = str(content.get("imageUrl") or "")
            if _DATA_IMAGE.match(image_url):
                artifacts.append(self._stage_data_image(image_url, filename_hint=f"image-{index + 1}"))
            elif image_url:
                parsed = urlparse(image_url)
                if parsed.scheme == "file":
                    artifacts.append(
                        self._stage_local(
                            self._file_url_path(parsed),
                            kind="image",
                        )
                    )
        return tuple(artifacts)

    @_serialized_stage
    def stage_upload(
        self,
        content: bytes,
        *,
        kind: str,
        content_type: str,
        filename: str,
    ) -> OutboundArtifact:
        """Stage bytes received by the authenticated local delivery endpoint."""

        safe_name = Path(str(filename or "").replace("\\", "/")).name
        if not safe_name or len(safe_name) > 120:
            raise ValueError("artifact filename must be between 1 and 120 characters")
        if kind not in {"image", "file"}:
            raise ValueError("artifact kind must be image or file")
        limit = _MAX_IMAGE_BYTES if kind == "image" else _MAX_FILE_BYTES
        if len(content) > limit:
            raise ValueError(f"{kind} output exceeds the delivery size limit")
        if kind == "file":
            # Explicit delivery accepts the same safely inspectable generic
            # file set as inbound IM attachments.
            from ..file_types import detect_generic_file

            detected_type, _suffix = detect_generic_file(safe_name, content)
            content_type = detected_type
        return self._stage_bytes(
            content,
            kind=kind,
            content_type=content_type or "application/octet-stream",
            filename=safe_name,
            unique=True,
        )

    @_serialized_stage
    def stage_markdown_images(self, text: str, *, cwd: str) -> tuple[OutboundArtifact, ...]:
        """Stage local images referenced by final-answer Markdown.

        Only actual image nodes outside Markdown code spans are delivery
        instructions. Ordinary links and image-looking examples inside code
        remain text. An explicit image may point at a native temporary output
        outside the thread workspace; Pillow still validates its bytes before
        the bridge stages it.
        """
        artifacts: list[OutboundArtifact] = []
        markdown = _mask_markdown_code(text)
        for match in _MARKDOWN_LINK.finditer(markdown):
            if match.group(1) != "!" or _is_escaped(markdown, match.start()):
                continue
            target = unquote(match.group(2) or match.group(3) or "").strip()
            parsed = urlparse(target)
            windows_absolute = len(target) >= 3 and target[1] == ":" and target[2] in {"/", "\\"}
            if parsed.scheme in {"http", "https", "data"} and not windows_absolute:
                continue
            if parsed.scheme == "file" and not windows_absolute:
                target = self._file_url_path(parsed)
            elif parsed.scheme and parsed.scheme != "sandbox" and not windows_absolute:
                continue
            elif parsed.scheme == "sandbox" and not windows_absolute:
                target = parsed.path
            candidate = Path(target)
            if not candidate.is_absolute():
                if not cwd:
                    raise ValueError("local output link has no native workspace root")
                candidate = Path(cwd) / candidate
            artifacts.append(
                self._stage_local(
                    candidate,
                    kind="image",
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _file_url_path(parsed) -> str:
        path = url2pathname(unquote(parsed.path))
        authority = str(parsed.netloc or "")
        if authority and authority.lower() != "localhost":
            return f"//{authority}{path}"
        if os.name == "nt" and re.match(r"^[\\/][A-Za-z]:", path):
            return path[1:]
        return path

    def _stage_data_image(self, value: str, *, filename_hint: str) -> OutboundArtifact:
        match = _DATA_IMAGE.match(value)
        if match is None:
            raise ValueError("unsupported image data URL")
        content_type = match.group(1).lower()
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 image output") from exc
        if len(content) > _MAX_IMAGE_BYTES:
            raise ValueError("image output exceeds the 10 MiB delivery limit")
        extension = mimetypes.guess_extension(content_type) or ".img"
        return self._stage_bytes(
            content,
            kind="image",
            content_type=content_type,
            filename=f"{filename_hint}{extension}",
        )

    def _stage_local(
        self,
        path: str | Path,
        *,
        kind: str,
    ) -> OutboundArtifact:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"output artifact is not a regular file: {source.name}")
        size = source.stat().st_size
        limit = _MAX_IMAGE_BYTES if kind == "image" else _MAX_FILE_BYTES
        if size > limit:
            raise ValueError(f"{kind} output exceeds the delivery size limit")
        content = source.read_bytes()
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self._stage_bytes(
            content,
            kind=kind,
            content_type=content_type,
            filename=source.name,
        )

    def _stage_bytes(
        self,
        content: bytes,
        *,
        kind: str,
        content_type: str,
        filename: str,
        unique: bool = False,
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
        target_name = (
            f"{digest}.{secrets.token_hex(8)}{suffix}"
            if unique
            else f"{digest}{suffix}"
        )
        target = self.root / target_name
        if target.exists():
            try:
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != digest
                ):
                    raise ValueError(
                        "managed outbound artifact content does not match its digest"
                    )
            except OSError as exc:
                raise ValueError("managed outbound artifact cannot be verified") from exc
        else:
            self._ensure_spool_capacity(len(content))
            temporary = self.root / f".{digest}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
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
                self._fsync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)
        if os.name != "nt":
            os.chmod(target, 0o600)
        artifact = OutboundArtifact(
            kind="image" if kind == "image" else "file",
            local_path=str(target.resolve()),
            content_type=content_type,
            filename=Path(filename).name or target.name,
            size_bytes=len(content),
            sha256=digest,
        )
        self._leased_paths.add(artifact.local_path)
        return artifact

    @_serialized
    def release(self, artifacts) -> None:
        for artifact in artifacts:
            path = str(getattr(artifact, "local_path", artifact) or "")
            if path:
                self._leased_paths.discard(path)

    @_serialized
    def cleanup_unreferenced(self, referenced_paths: set[str]) -> None:
        """Remove stale spool entries at startup while preserving durable outbox refs."""
        if not self.root.exists():
            return
        referenced: set[Path] = set()
        for value in {*referenced_paths, *self._leased_paths}:
            try:
                candidate = Path(value).resolve(strict=False)
                candidate.relative_to(self.root.resolve())
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

    def _ensure_spool_capacity(self, incoming_bytes: int) -> None:
        total = 0
        try:
            for candidate in self.root.iterdir():
                if candidate.is_file():
                    total += candidate.stat().st_size
        except OSError as exc:
            raise ValueError("managed outbound artifact spool cannot be measured") from exc
        if total + incoming_bytes > _MAX_SPOOL_BYTES:
            raise ValueError("outbound artifact spool exceeds the 256 MiB limit")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _mask_markdown_code(text: str) -> str:
    """Blank fenced and inline code while preserving source offsets."""

    masked = [False] * len(text)
    fence_char = ""
    fence_size = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _FENCE_OPEN.match(body)
        if fence_char:
            masked[offset : offset + len(line)] = [True] * len(line)
            if match is not None:
                marker = match.group(1)
                suffix = match.group(2)
                if (
                    marker[0] == fence_char
                    and len(marker) >= fence_size
                    and not suffix.strip()
                ):
                    fence_char = ""
                    fence_size = 0
        elif match is not None:
            marker = match.group(1)
            suffix = match.group(2)
            if marker[0] != "`" or "`" not in suffix:
                fence_char = marker[0]
                fence_size = len(marker)
                masked[offset : offset + len(line)] = [True] * len(line)
        offset += len(line)

    index = 0
    while index < len(text):
        if masked[index] or text[index] != "`":
            index += 1
            continue
        run_end = index + 1
        while run_end < len(text) and not masked[run_end] and text[run_end] == "`":
            run_end += 1
        run_size = run_end - index
        cursor = run_end
        closing_end = -1
        while cursor < len(text):
            if masked[cursor] or text[cursor] != "`":
                cursor += 1
                continue
            candidate_end = cursor + 1
            while (
                candidate_end < len(text)
                and not masked[candidate_end]
                and text[candidate_end] == "`"
            ):
                candidate_end += 1
            if candidate_end - cursor == run_size:
                closing_end = candidate_end
                break
            cursor = candidate_end
        if closing_end < 0:
            index = run_end
            continue
        masked[index:closing_end] = [True] * (closing_end - index)
        index = closing_end

    return "".join(
        " " if hidden else char
        for char, hidden in zip(text, masked, strict=True)
    )


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1

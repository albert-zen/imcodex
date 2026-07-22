from __future__ import annotations

from pathlib import Path
import re


TEXT_FILE_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/jsx",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".css": "text/css",
    ".scss": "text/x-scss",
    ".less": "text/x-less",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".fish": "text/x-shellscript",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cc": "text/x-c++",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".java": "text/x-java-source",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".sql": "application/sql",
    ".graphql": "application/graphql",
    ".proto": "text/x-protobuf",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
}


class UnsupportedGenericFileError(ValueError):
    pass


class InvalidGenericFileError(ValueError):
    pass


def detect_generic_file(filename: str, content: bytes) -> tuple[str, str]:
    """Detect the supported safe file subset from filename plus actual bytes."""

    suffix = Path(filename).suffix.casefold()
    if suffix == ".pdf":
        tail = content[-2048:].rstrip()
        if (
            re.match(rb"%PDF-[12]\.\d(?:\r?\n|\r)", content) is None
            or re.search(rb"\d+\s+\d+\s+obj\b", content) is None
            or b"endobj" not in content
            or b"startxref" not in tail
            or not tail.endswith(b"%%EOF")
            or (
                re.search(rb"(?:^|\r?\n)xref(?:\r?\n|\r)", content) is None
                and re.search(rb"/Type\s*/XRef\b", content) is None
            )
        ):
            raise InvalidGenericFileError
        return "application/pdf", suffix
    content_type = TEXT_FILE_CONTENT_TYPES.get(suffix)
    if content_type is None:
        raise UnsupportedGenericFileError
    if b"\x00" in content:
        raise InvalidGenericFileError
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidGenericFileError from None
    return content_type, suffix

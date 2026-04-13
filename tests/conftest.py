from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TMP_ROOT = ROOT / ".tmp-pytest"
TMP_ROOT.mkdir(exist_ok=True)
for name in ("TMPDIR", "TEMP", "TMP"):
    os.environ[name] = str(TMP_ROOT)
tempfile.tempdir = str(TMP_ROOT)


@pytest.fixture
def tmp_path() -> Path:
    path = TMP_ROOT / f"case-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        path.rmdir()


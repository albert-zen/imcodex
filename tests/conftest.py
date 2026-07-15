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
    original_cwd = os.getcwd()
    try:
        yield path
    finally:
        # Windows keeps the current working directory open, so a test whose
        # monkeypatch fixture tears down after tmp_path would otherwise make
        # this directory impossible to remove.
        current_cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        temporary_root = os.path.normcase(os.path.abspath(str(path)))
        try:
            cwd_is_temporary = os.path.commonpath((current_cwd, temporary_root)) == temporary_root
        except ValueError:
            cwd_is_temporary = False
        if cwd_is_temporary:
            os.chdir(original_cwd if os.path.exists(original_cwd) else str(ROOT))
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        path.rmdir()
        # The shared media quota lock intentionally lives beside the spool so
        # it remains available while the spool directory is replaced. Test
        # cases use a unique tmp_path parent, so remove their sibling locks too.
        for lock_path in path.parent.glob(f".{path.name}*.imcodex-spool.lock"):
            lock_path.unlink(missing_ok=True)

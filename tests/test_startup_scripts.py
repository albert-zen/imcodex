from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
@pytest.mark.parametrize("endpoint", ["unix://", "stdio://"])
def test_start_sh_does_not_start_legacy_core_for_a_canonical_target(
    endpoint: str,
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    capture_path = tmp_path / "python-invocations.txt"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$IMCODEX_TEST_CAPTURE"\n'
        'case "$*" in *"core start"*) exit 42 ;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    for name in ("IMCODEX_CONDA_ENV", "IMCODEX_CORE_MODE", "IMCODEX_CORE_URL"):
        environment.pop(name, None)
    environment.update(
        {
            "IMCODEX_APP_SERVER_URL": endpoint,
            "IMCODEX_PYTHON": str(fake_python),
            "IMCODEX_TEST_CAPTURE": str(capture_path),
        }
    )

    completed = subprocess.run(
        ["bash", "scripts/start.sh"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"App Server target: {endpoint}" in completed.stdout
    assert capture_path.read_text(encoding="utf-8").splitlines() == ["-m imcodex"]


def test_start_ps1_guards_legacy_core_start_when_canonical_target_is_set() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert 'Write-Host "App Server target: $appServerUrl"' in script
    assert 'if (-not $appServerUrl -and $coreMode -eq "dedicated-ws")' in script


def test_doctor_uses_the_canonical_target_resolver_without_claiming_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "load_app_server_target" in script
    assert '"unix-websocket"' in script
    assert '"stdio-jsonl"' in script
    assert "shared-ws probe + stdio fallback" not in script

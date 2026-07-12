from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "doctor.ps1"


def test_windows_doctor_matches_launcher_interpreter_and_target_model() -> None:
    script = DOCTOR_SCRIPT.read_text(encoding="utf-8")

    assert ".venv\\Scripts\\python.exe" in script
    assert ".venv/bin/python" in script
    assert "from imcodex.config import load_app_server_target" in script
    assert 'Get-Setting "IMCODEX_DATA_DIR" ".imcodex"' in script
    assert "Unix control sockets require WSL/macOS/Linux" in script
    assert "python -m imcodex channels doctor" in script
    assert "[Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT" in script
    assert "if (-not $isNativeWindows)" in script
    assert "app-server daemon --help" in script
    assert 'Get-Setting "IMCODEX_HTTP_HOST" "0.0.0.0"' in script
    assert "Test-PortAvailable -HostName $httpHost -Port $httpPort" in script
    assert "??" not in script
    assert "shared-ws probe + stdio fallback" not in script


def test_windows_doctor_parses_in_windows_powershell_when_available() -> None:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available on this platform")
    parser = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$args[0], [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )

    subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-Command", parser, str(DOCTOR_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )

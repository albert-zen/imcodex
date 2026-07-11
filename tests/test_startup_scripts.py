from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


_TARGET_ENV_NAMES = (
    "IMCODEX_APP_SERVER_URL",
    "IMCODEX_CORE_URL",
    "IMCODEX_CORE_MODE",
    "IMCODEX_CORE_PORT",
)
_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _run_start_sh(
    tmp_path: Path,
    *,
    dotenv: str = "",
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(source_root / "scripts" / "start.sh", scripts_dir / "start.sh")
    (repo_root / ".env").write_text(dotenv, encoding="utf-8")

    capture_path = tmp_path / "python-invocations.txt"
    target_capture_path = tmp_path / "target-environment.txt"
    core_started_path = tmp_path / "legacy-core-started"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then\n'
        '  printf "%s\\n" "$*" >> "$IMCODEX_TEST_CAPTURE"\n'
        "fi\n"
        'case "$*" in\n'
        '  *"app-server start"*) exit "${IMCODEX_TEST_DAEMON_EXIT:-0}" ;;\n'
        '  *"core start"*) touch "$IMCODEX_TEST_CORE_STARTED"; exit 0 ;;\n'
        '  -\\ *) if [ -f "$IMCODEX_TEST_CORE_STARTED" ]; then exit 0; else exit 1; fi ;;\n'
        "esac\n"
        'if [ "$*" = "-m imcodex" ]; then\n'
        '  printf "%s|%s|%s|%s\\n" "${IMCODEX_APP_SERVER_URL:-}" '
        '"${IMCODEX_CORE_URL:-}" "${IMCODEX_CORE_MODE:-}" '
        '"${IMCODEX_CORE_PORT:-}" > "$IMCODEX_TEST_TARGET_CAPTURE"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    environment = os.environ.copy()
    for name in (
        *_TARGET_ENV_NAMES,
        "IMCODEX_CONDA_ENV",
        "IMCODEX_CORE_START_TIMEOUT",
        "IMCODEX_PYTHON",
        "IMCODEX_TEST_CAPTURE",
        "IMCODEX_TEST_CORE_STARTED",
        "IMCODEX_TEST_DAEMON_EXIT",
        "IMCODEX_TEST_TARGET_CAPTURE",
        "CONDA_EXE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "IMCODEX_PYTHON": str(fake_python),
            "IMCODEX_TEST_CAPTURE": str(capture_path),
            "IMCODEX_TEST_CORE_STARTED": str(core_started_path),
            "IMCODEX_TEST_TARGET_CAPTURE": str(target_capture_path),
        }
    )
    environment.update(environment_overrides or {})

    completed = subprocess.run(
        ["bash", "scripts/start.sh"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    invocations = (
        capture_path.read_text(encoding="utf-8").splitlines()
        if capture_path.exists()
        else []
    )
    target_environment = (
        target_capture_path.read_text(encoding="utf-8").strip()
        if target_capture_path.exists()
        else ""
    )
    return completed, invocations, target_environment


def _fake_conda_executable(tmp_path: Path) -> Path:
    conda_root = tmp_path / "fake-conda"
    conda_executable = conda_root / "bin" / "conda"
    conda_hook = conda_root / "etc" / "profile.d" / "conda.sh"
    conda_executable.parent.mkdir(parents=True)
    conda_hook.parent.mkdir(parents=True)
    conda_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    conda_executable.chmod(0o755)
    conda_hook.write_text(
        "conda() {\n"
        '  if [ "$1" = "activate" ]; then\n'
        "    export IMCODEX_CORE_MODE=spawned-stdio\n"
        "  fi\n"
        "}\n",
        encoding="utf-8",
    )
    return conda_executable


def _run_start_ps1(
    tmp_path: Path,
    *,
    dotenv: str = "",
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    assert _POWERSHELL is not None
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(source_root / "scripts" / "start.ps1", scripts_dir / "start.ps1")
    (repo_root / ".env").write_text(dotenv, encoding="utf-8")

    capture_path = tmp_path / "powershell-python-invocations.txt"
    target_capture_path = tmp_path / "powershell-target-environment.txt"
    if os.name == "nt":
        fake_python = tmp_path / "fake-python.cmd"
        fake_python.write_text(
            "@echo off\n"
            "echo %*>>\"%IMCODEX_TEST_CAPTURE%\"\n"
            "if \"%*\"==\"-m imcodex\" (\n"
            "  >\"%IMCODEX_TEST_TARGET_CAPTURE%\" echo "
            "%IMCODEX_APP_SERVER_URL%^|%IMCODEX_CORE_URL%^|"
            "%IMCODEX_CORE_MODE%^|%IMCODEX_CORE_PORT%\n"
            ")\n"
            "exit /b 0\n",
            encoding="utf-8",
        )
    else:
        fake_python = tmp_path / "fake-python"
        fake_python.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$IMCODEX_TEST_CAPTURE"\n'
            'if [ "$*" = "-m imcodex" ]; then\n'
            '  printf "%s|%s|%s|%s\\n" "${IMCODEX_APP_SERVER_URL:-}" '
            '"${IMCODEX_CORE_URL:-}" "${IMCODEX_CORE_MODE:-}" '
            '"${IMCODEX_CORE_PORT:-}" > "$IMCODEX_TEST_TARGET_CAPTURE"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

    environment = os.environ.copy()
    for name in (
        *_TARGET_ENV_NAMES,
        "IMCODEX_CONDA_ENV",
        "IMCODEX_CORE_START_TIMEOUT",
        "IMCODEX_PYTHON",
        "IMCODEX_TEST_CAPTURE",
        "IMCODEX_TEST_TARGET_CAPTURE",
        "CONDA_EXE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "IMCODEX_PYTHON": str(fake_python),
            "IMCODEX_TEST_CAPTURE": str(capture_path),
            "IMCODEX_TEST_TARGET_CAPTURE": str(target_capture_path),
        }
    )
    environment.update(environment_overrides or {})

    completed = subprocess.run(
        [
            _POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "start.ps1"),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    invocations = (
        capture_path.read_text(encoding="utf-8").splitlines()
        if capture_path.exists()
        else []
    )
    target_environment = (
        target_capture_path.read_text(encoding="utf-8").strip()
        if target_capture_path.exists()
        else ""
    )
    return completed, invocations, target_environment


def _fake_powershell_conda_executable(tmp_path: Path) -> Path:
    conda_root = tmp_path / "fake-powershell-conda"
    conda_executable = conda_root / "bin" / "conda"
    conda_hook = conda_root / "shell" / "condabin" / "conda-hook.ps1"
    conda_executable.parent.mkdir(parents=True)
    conda_hook.parent.mkdir(parents=True)
    conda_executable.write_text("", encoding="utf-8")
    hook_source = (
        "function global:conda {\n"
        '    if ($args[0] -eq "activate") {\n'
        '        $env:IMCODEX_CORE_MODE = "spawned-stdio"\n'
        "    }\n"
        "}\n"
    )
    conda_hook.write_text(hook_source, encoding="utf-8")
    if os.name != "nt":
        alternate_hook = conda_root / r"shell\condabin\conda-hook.ps1"
        alternate_hook.write_text(hook_source, encoding="utf-8")
    return conda_executable


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
@pytest.mark.parametrize("endpoint", ["unix://", "stdio://", "ws://127.0.0.1:9900"])
def test_start_sh_does_not_start_legacy_core_for_a_canonical_target(
    endpoint: str,
    tmp_path,
) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        environment_overrides={"IMCODEX_APP_SERVER_URL": endpoint},
    )

    assert completed.returncode == 0, completed.stderr
    assert f"App Server target: {endpoint}" in completed.stdout
    assert invocations == ["-m imcodex"]
    assert target_environment == f"{endpoint}|||"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_defaults_to_ensuring_the_native_daemon_then_starts_the_bridge(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "App Server target: unix://" in completed.stdout
    assert "Ensuring native Codex App Server daemon is running" in completed.stdout
    assert invocations == ["-m imcodex app-server start", "-m imcodex"]
    assert target_environment == "unix://|||"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_treats_a_dotenv_canonical_target_as_connect_only(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        dotenv="IMCODEX_APP_SERVER_URL=unix:///tmp/codex-control.sock\n",
    )

    assert completed.returncode == 0, completed.stderr
    assert invocations == ["-m imcodex"]
    assert target_environment == "unix:///tmp/codex-control.sock|||"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_propagates_native_daemon_failure_without_starting_the_bridge(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        environment_overrides={"IMCODEX_TEST_DAEMON_EXIT": "17"},
    )

    assert completed.returncode == 17
    assert invocations == ["-m imcodex app-server start"]
    assert target_environment == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_preserves_explicit_legacy_dedicated_core_configuration(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        environment_overrides={
            "IMCODEX_CORE_MODE": "dedicated-ws",
            "IMCODEX_CORE_PORT": "9123",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert "Legacy core mode: dedicated-ws" in completed.stdout
    assert invocations == ["-m imcodex core start --port 9123", "-m imcodex"]
    assert target_environment == "|ws://127.0.0.1:9123|dedicated-ws|9123"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_never_starts_legacy_core_when_canonical_and_legacy_values_conflict(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        environment_overrides={
            "IMCODEX_APP_SERVER_URL": "unix://",
            "IMCODEX_CORE_MODE": "dedicated-ws",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert invocations == ["-m imcodex"]
    assert target_environment == "unix://||dedicated-ws|"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_keeps_process_target_group_separate_from_dotenv_target_group(tmp_path) -> None:
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        dotenv="IMCODEX_APP_SERVER_URL=unix://\nIMCODEX_CORE_PORT=8765\n",
        environment_overrides={"IMCODEX_CORE_MODE": "spawned-stdio"},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Legacy core mode: spawned-stdio" in completed.stdout
    assert invocations == ["-m imcodex"]
    assert target_environment == "||spawned-stdio|"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_prefers_conda_injected_target_group_over_dotenv_target_group(tmp_path) -> None:
    conda_executable = _fake_conda_executable(tmp_path)
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        dotenv=(
            "IMCODEX_CONDA_ENV=imcodex-test\n"
            "IMCODEX_APP_SERVER_URL=unix://\n"
        ),
        environment_overrides={"CONDA_EXE": str(conda_executable)},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Legacy core mode: spawned-stdio" in completed.stdout
    assert invocations == ["-m imcodex"]
    assert target_environment == "||spawned-stdio|"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_start_sh_restores_entry_process_target_group_after_conda_activation(tmp_path) -> None:
    conda_executable = _fake_conda_executable(tmp_path)
    completed, invocations, target_environment = _run_start_sh(
        tmp_path,
        dotenv="IMCODEX_CONDA_ENV=imcodex-test\n",
        environment_overrides={
            "CONDA_EXE": str(conda_executable),
            "IMCODEX_APP_SERVER_URL": "unix://",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert "App Server target: unix://" in completed.stdout
    assert invocations == ["-m imcodex"]
    assert target_environment == "unix://|||"


def test_start_ps1_guards_legacy_core_start_when_canonical_target_is_set() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert 'Write-Host "App Server target: $appServerUrl"' in script
    assert '$appServerUrl = "stdio://"' in script
    assert '$env:IMCODEX_APP_SERVER_URL = $appServerUrl' in script
    assert '$script:PreActivationTargetConfigured' in script
    assert "Resolve-TargetEnvironment" in script
    assert "Import-DotEnv\nEnable-CondaEnv\nResolve-TargetEnvironment" in script
    assert 'if (-not $appServerUrl -and $legacyCoreConfigured -and $coreMode -eq "dedicated-ws")' in script


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is not available")
@pytest.mark.parametrize(
    ("dotenv", "environment_overrides", "expected_target"),
    [
        ("", {}, "stdio://|||"),
        ("", {"IMCODEX_APP_SERVER_URL": "unix://"}, "unix://|||"),
        ("", {"IMCODEX_CORE_MODE": "spawned-stdio"}, "||spawned-stdio|"),
        (
            "IMCODEX_APP_SERVER_URL=unix://\nIMCODEX_CORE_PORT=8765\n",
            {"IMCODEX_CORE_MODE": "spawned-stdio"},
            "||spawned-stdio|",
        ),
    ],
)
def test_start_ps1_executes_target_precedence_matrix(
    tmp_path,
    dotenv: str,
    environment_overrides: dict[str, str],
    expected_target: str,
) -> None:
    completed, invocations, target_environment = _run_start_ps1(
        tmp_path,
        dotenv=dotenv,
        environment_overrides=environment_overrides,
    )

    assert completed.returncode == 0, completed.stderr
    assert invocations == ["-m imcodex"]
    assert target_environment == expected_target


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is not available")
@pytest.mark.parametrize(
    ("entry_process_target", "expected_target"),
    [
        ({}, "||spawned-stdio|"),
        ({"IMCODEX_APP_SERVER_URL": "unix://"}, "unix://|||"),
    ],
)
def test_start_ps1_keeps_conda_target_group_separate(
    tmp_path,
    entry_process_target: dict[str, str],
    expected_target: str,
) -> None:
    conda_executable = _fake_powershell_conda_executable(tmp_path)
    completed, invocations, target_environment = _run_start_ps1(
        tmp_path,
        dotenv=(
            "IMCODEX_CONDA_ENV=imcodex-test\n"
            "IMCODEX_APP_SERVER_URL=unix://\n"
        ),
        environment_overrides={
            "CONDA_EXE": str(conda_executable),
            **entry_process_target,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert invocations == ["-m imcodex"]
    assert target_environment == expected_target


def test_doctor_uses_the_canonical_target_resolver_without_claiming_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "load_app_server_target" in script
    assert '"unix-websocket"' in script
    assert '"stdio-jsonl"' in script
    assert "shared-ws probe + stdio fallback" not in script
    assert '$env:IMCODEX_APP_SERVER_URL = "stdio://"' in script
    assert '$selectedCoreModeNormalized -in @("", "dedicated-ws")' in script
    assert '$env:IMCODEX_CORE_URL = "ws://127.0.0.1:$($selectedCorePort.Trim())"' in script

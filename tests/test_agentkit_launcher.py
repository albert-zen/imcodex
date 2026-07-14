from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash is not available")
def test_posix_agentkit_launcher_uses_python_module_and_repo_root(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    capture_path = tmp_path / "args.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-P" ] && [ "$2" = "-c" ]; then exit 0; fi\n'
        'printf "%s\\n" "$@" > "$AGENTKIT_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTKIT_PYTHON": str(fake_python),
            "AGENTKIT_TEST_CAPTURE": str(capture_path),
        }
    )

    completed = subprocess.run(
        ["bash", str(repo_root / "scripts" / "agentkit"), "status"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "-P",
        "-m",
        "agentkit",
        "--repo",
        str(repo_root),
        "status",
    ]


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash is not available")
def test_posix_agentkit_launcher_ignores_cwd_module_shadowing(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    package_root = tmp_path / "installed"
    package = package_root / "agentkit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['AGENTKIT_TEST_CAPTURE']).write_text(\n"
        "    '\\n'.join(sys.argv[1:]), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    shadow_root = tmp_path / "shadow"
    shadow_root.mkdir()
    shadow_marker = tmp_path / "shadow-imported"
    (shadow_root / "agentkit.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(shadow_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "raise RuntimeError('the cwd module must not be imported')\n",
        encoding="utf-8",
    )
    capture_path = tmp_path / "args.txt"
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTKIT_PYTHON": sys.executable,
            "AGENTKIT_TEST_CAPTURE": str(capture_path),
            "PYTHONPATH": str(package_root),
        }
    )

    completed = subprocess.run(
        ["bash", str(repo_root / "scripts" / "agentkit"), "status"],
        cwd=shadow_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not shadow_marker.exists()
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "--repo",
        str(repo_root),
        "status",
    ]


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash is not available")
def test_posix_agentkit_launcher_explains_pipless_install(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["AGENTKIT_PYTHON"] = str(fake_python)

    completed = subprocess.run(
        ["bash", str(repo_root / "scripts" / "agentkit"), "status"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 127
    assert "uv pip install --python" in completed.stderr
    assert "-m ensurepip --upgrade" in completed.stderr


def test_windows_agentkit_launcher_uses_python_module() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "scripts" / "agentkit.cmd").read_text(encoding="utf-8")

    assert ".venv\\Scripts\\python.exe" in source
    assert '"%PYTHON%" -P -c "import agentkit.__main__"' in source
    assert '"%PYTHON%" -P -m agentkit --repo "%REPO_ROOT%" %*' in source
    assert 'uv pip install --python "%PYTHON%"' in source
    assert 'From "%REPO_ROOT%", prefer:' in source
    assert "uv run" not in source


@pytest.mark.skipif(os.name != "nt", reason="native cmd.exe is not available")
def test_windows_agentkit_launcher_quotes_missing_install_diagnostics(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo & tools (copy)"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(
        source_root / "scripts" / "agentkit.cmd",
        scripts_dir / "agentkit.cmd",
    )
    venv.EnvBuilder(with_pip=False).create(repo_root / ".venv")
    environment = os.environ.copy()
    environment.pop("AGENTKIT_PYTHON", None)
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        f'cmd.exe /d /s /c call "{scripts_dir / "agentkit.cmd"}" status',
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 127
    assert "AgentKit is not installed" in completed.stderr
    assert "repo & tools (copy)" in completed.stderr
    assert "uv pip install --python" in completed.stderr
    assert "was unexpected" not in completed.stderr


def test_bundled_agentkit_hook_uses_repository_launcher() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    hooks = json.loads(
        (repo_root / "plugins" / "agentkit" / "hooks.json").read_text(encoding="utf-8")
    )

    command = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    command_windows = hooks["hooks"]["Stop"][0]["hooks"][0]["commandWindows"]
    assert command == '"${PLUGIN_ROOT}/hooks/run-stop.sh"'
    assert command_windows == 'call "${PLUGIN_ROOT}\\hooks\\run-stop.cmd"'


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash is not available")
def test_posix_agentkit_hook_finds_repo_launcher_from_nested_cwd(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo & tools"
    nested = repo_root / "packages" / "feature"
    scripts_dir = repo_root / "scripts"
    nested.mkdir(parents=True)
    scripts_dir.mkdir()
    capture_path = tmp_path / "hook-args.json"
    launcher = scripts_dir / "agentkit"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import json, os, sys; "
        "open(os.environ[\"AGENTKIT_HOOK_CAPTURE\"], \"w\").write(json.dumps(sys.argv[1:]))' "
        '"$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    environment = os.environ.copy()
    environment["AGENTKIT_HOOK_CAPTURE"] = str(capture_path)

    completed = subprocess.run(
        ["bash", str(source_root / "plugins" / "agentkit" / "hooks" / "run-stop.sh")],
        cwd=nested,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(capture_path.read_text(encoding="utf-8")) == [
        "codex-stop-hook",
        "--log",
        str(repo_root / ".agentkit" / "codex-stop-hook.log"),
    ]


def test_windows_agentkit_hook_wrapper_searches_parent_directories() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "plugins" / "agentkit" / "hooks" / "run-stop.cmd").read_text(
        encoding="utf-8"
    )

    assert 'if exist "%REPO_ROOT%\\scripts\\agentkit.cmd" goto run_hook' in source
    assert 'call "%REPO_ROOT%\\scripts\\agentkit.cmd" codex-stop-hook' in source

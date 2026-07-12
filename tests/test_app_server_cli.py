from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import imcodex.main as main_module
from imcodex.app_server_cli import resolve_native_command, run_app_server_cli
from imcodex.config import load_codex_bin


@pytest.mark.parametrize(
    ("project_command", "native_command"),
    [
        ("start", "start"),
        ("restart", "restart"),
        ("stop", "stop"),
        ("status", "version"),
    ],
)
def test_app_server_cli_delegates_to_the_native_daemon(
    project_command: str,
    native_command: str,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def process_runner(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f'{{"command":"{native_command}"}}\n',
            stderr="",
        )

    exit_code = run_app_server_cli(
        [project_command],
        stdout=stdout,
        stderr=stderr,
        codex_bin="custom-codex",
        process_runner=process_runner,
        os_name="posix",
    )

    assert exit_code == 0
    assert calls == [
        (
            ["custom-codex", "app-server", "daemon", "--help"],
            {
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
        ),
        (
            ["custom-codex", "app-server", "daemon", native_command],
            {
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
        )
    ]
    assert stdout.getvalue() == f'{{"command":"{native_command}"}}\n'
    assert stderr.getvalue() == ""


def test_app_server_status_preserves_native_failure_and_stderr() -> None:
    stderr = io.StringIO()

    def process_runner(command: list[str], **_kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout="daemon help", stderr="")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="failed to connect to the native control socket\n",
        )

    exit_code = run_app_server_cli(
        ["status"],
        stdout=io.StringIO(),
        stderr=stderr,
        codex_bin="codex",
        process_runner=process_runner,
        os_name="posix",
    )

    assert exit_code == 1
    assert stderr.getvalue() == "failed to connect to the native control socket\n"


def test_app_server_cli_reads_codex_bin_without_loading_unrelated_settings(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "IMCODEX_CODEX_BIN=dotenv-codex\nIMCODEX_HTTP_PORT=not-an-integer\n",
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    def process_runner(command: list[str], **_kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert run_app_server_cli(["start"], process_runner=process_runner, os_name="posix") == 0
    assert captured == [
        ["dotenv-codex", "app-server", "daemon", "--help"],
        ["dotenv-codex", "app-server", "daemon", "start"],
    ]
    assert not (tmp_path / ".imcodex-core").exists()
    assert not (tmp_path / ".imcodex-run").exists()


def test_load_codex_bin_prefers_process_environment(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("IMCODEX_CODEX_BIN=dotenv-codex\n", encoding="utf-8")
    monkeypatch.setenv("IMCODEX_CODEX_BIN", "environment-codex")

    assert load_codex_bin(dotenv) == "environment-codex"


def test_resolve_native_command_wraps_windows_cmd_shims() -> None:
    command = ["codex", "app-server", "daemon", "status"]

    assert resolve_native_command(
        command,
        os_name="nt",
        which=lambda name: r"C:\tools\codex.cmd" if name == "codex.cmd" else None,
    ) == ["cmd.exe", "/c", r"C:\tools\codex.cmd", *command[1:]]
    assert resolve_native_command(
        [r"C:\tools\codex.cmd", *command[1:]],
        os_name="nt",
    ) == ["cmd.exe", "/c", r"C:\tools\codex.cmd", *command[1:]]
    assert resolve_native_command(
        [r"C:\tools\codex.exe", *command[1:]],
        os_name="nt",
    ) == [r"C:\tools\codex.exe", *command[1:]]


def test_app_server_cli_rejects_native_daemon_lifecycle_on_windows() -> None:
    stderr = io.StringIO()

    def process_runner(_command: list[str], **_kwargs):
        raise AssertionError("native daemon command must not run on Windows")

    exit_code = run_app_server_cli(
        ["start"],
        stderr=stderr,
        process_runner=process_runner,
        os_name="nt",
    )

    assert exit_code == 2
    assert "daemon lifecycle is Unix-only" in stderr.getvalue()
    assert "scripts/start.ps1" in stderr.getvalue()


def test_app_server_cli_reports_missing_daemon_capability() -> None:
    stderr = io.StringIO()

    def process_runner(command: list[str], **_kwargs):
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr="unrecognized subcommand 'daemon'\n",
        )

    exit_code = run_app_server_cli(
        ["start"],
        stderr=stderr,
        process_runner=process_runner,
        os_name="posix",
    )

    assert exit_code == 2
    assert "does not provide native app-server daemon lifecycle" in stderr.getvalue()
    assert "unrecognized subcommand" in stderr.getvalue()


@pytest.mark.parametrize(
    ("exception", "expected_code", "message"),
    [
        (FileNotFoundError("missing codex"), 127, "Codex executable was not found"),
        (PermissionError("not executable"), 126, "failed to execute the Codex daemon command"),
    ],
)
def test_app_server_cli_reports_process_launch_failures(
    exception: OSError,
    expected_code: int,
    message: str,
) -> None:
    stderr = io.StringIO()

    def process_runner(_command: list[str], **_kwargs):
        raise exception

    exit_code = run_app_server_cli(
        ["status"],
        stderr=stderr,
        codex_bin="missing-codex",
        process_runner=process_runner,
        os_name="posix",
    )

    assert exit_code == expected_code
    assert message in stderr.getvalue()


def test_main_dispatches_app_server_commands_and_returns_their_exit_code(monkeypatch) -> None:
    captured: list[list[str]] = []

    def run_app_server_cli(argv: list[str]) -> int:
        captured.append(argv)
        return 19

    monkeypatch.setattr(main_module, "run_app_server_cli", run_app_server_cli)

    assert main_module.run(["app-server", "status"]) == 19
    assert captured == [["status"]]


def test_python_module_entrypoint_preserves_the_app_server_exit_code() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["IMCODEX_CODEX_BIN"] = str(repo_root / "definitely-missing-codex")

    completed = subprocess.run(
        [sys.executable, "-m", "imcodex", "app-server", "status"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    if os.name == "nt":
        assert completed.returncode == 2
        assert "daemon lifecycle is Unix-only" in completed.stderr
    else:
        assert completed.returncode == 127
        assert "Codex executable was not found" in completed.stderr

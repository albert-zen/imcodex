from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from imcodex.channels_cli import _send, _settings_from_bridge_root, run_channels_cli


def test_channels_send_posts_workspace_artifact_to_running_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    current = run_dir / "current"
    current.mkdir(parents=True)
    (current / "health.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "http": {"listening": True, "host": "0.0.0.0", "port": 8123},
            }
        ),
        encoding="utf-8",
    )
    (current / "delivery-token").write_text("delivery-secret\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.md"
    artifact.write_text("# Result\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"status": "delivered", "delivery_id": "stable-1"},
        )

    monkeypatch.setattr("imcodex.channels_cli.httpx.post", fake_post)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=run_dir),
        channel_id="telegram",
        conversation_id="chat:1",
        text_value="done",
        artifact_values=["result.md"],
        delivery_id="stable-1",
        output=output.append,
    )

    assert status == 0
    assert captured["url"] == "http://127.0.0.1:8123/_imcodex/tools/deliver"
    assert captured["headers"]["x-imcodex-instance"] == "instance-1"
    assert captured["headers"]["x-imcodex-delivery-token"] == "delivery-secret"
    payload = json.loads(captured["data"]["payload"])
    assert payload["delivery_id"] == "stable-1"
    assert payload["artifacts"][0]["kind"] == "file"
    assert json.loads(output[0])["status"] == "delivered"


def test_channels_send_accepts_explicit_artifact_outside_current_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    current = run_dir / "current"
    current.mkdir(parents=True)
    (current / "health.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "http": {"listening": True, "host": "127.0.0.1", "port": 8123},
            }
        ),
        encoding="utf-8",
    )
    (current / "delivery-token").write_text("delivery-secret\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.chdir(workspace)
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"status": "delivered", "delivery_id": "outside-1"},
        )

    monkeypatch.setattr("imcodex.channels_cli.httpx.post", fake_post)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=run_dir),
        channel_id="telegram",
        conversation_id="chat:1",
        text_value="",
        artifact_values=[str(outside)],
        delivery_id="outside-1",
        output=output.append,
    )

    assert status == 0
    assert captured["files"][0][1][0] == "secret.md"
    assert captured["files"][0][1][1] == b"secret"


def test_channels_send_reports_bridge_http_error_with_nonempty_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    current = run_dir / "current"
    current.mkdir(parents=True)
    (current / "health.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "http": {"listening": True, "host": "127.0.0.1", "port": 8123},
            }
        ),
        encoding="utf-8",
    )
    (current / "delivery-token").write_text("delivery-secret\n", encoding="utf-8")

    def fake_post(url, **_kwargs):
        return httpx.Response(
            422,
            request=httpx.Request("POST", url),
            json={"detail": "unsupported generic file type"},
        )

    monkeypatch.setattr("imcodex.channels_cli.httpx.post", fake_post)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=run_dir),
        channel_id="telegram",
        conversation_id="chat:1",
        text_value="done",
        artifact_values=[],
        delivery_id="unsupported-1",
        output=output.append,
    )

    assert status == 1
    assert json.loads(output[0]) == {
        "status": "failed",
        "http_status": 422,
        "error": "unsupported generic file type",
    }


def test_channels_send_current_posts_source_thread_without_explicit_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    current = run_dir / "current"
    current.mkdir(parents=True)
    (current / "health.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "http": {"listening": True, "host": "127.0.0.1", "port": 8123},
            }
        ),
        encoding="utf-8",
    )
    (current / "delivery-token").write_text("delivery-secret\n", encoding="utf-8")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status": "delivered",
                "delivery_id": "stable-current",
                "channel_id": "qq",
                "conversation_id": "c2c:user",
            },
        )

    monkeypatch.setattr("imcodex.channels_cli.httpx.post", fake_post)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=run_dir),
        channel_id="",
        conversation_id="",
        source_thread_id="thread-current",
        text_value="done",
        artifact_values=[],
        delivery_id="stable-current",
        output=output.append,
    )

    assert status == 0
    payload = json.loads(captured["data"]["payload"])
    assert payload["source_thread_id"] == "thread-current"
    assert "channel_id" not in payload
    assert "conversation_id" not in payload
    assert json.loads(output[0])["channel_id"] == "qq"


def test_channels_send_current_reads_native_thread_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-current")
    captured = {}

    def fake_send(_settings, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("imcodex.channels_cli._send", fake_send)

    status = run_channels_cli(
        ["send", "--current", "--text", "done"],
        settings=SimpleNamespace(),
        output=lambda _value: None,
    )

    assert status == 0
    assert captured["source_thread_id"] == "thread-current"
    assert captured["channel_id"] == ""
    assert captured["conversation_id"] == ""


def test_channels_send_current_fails_without_native_thread_environment(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    output: list[str] = []

    status = run_channels_cli(
        ["send", "--current", "--text", "done"],
        settings=SimpleNamespace(),
        output=output.append,
    )

    assert status == 2
    assert "CODEX_THREAD_ID is unavailable" in json.loads(output[0])["error"]


def test_send_settings_load_from_bridge_root_without_changing_artifact_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir()
    (bridge_root / ".env").write_text("IMCODEX_RUN_DIR=.runtime\n", encoding="utf-8")
    artifact_root = tmp_path / "workspace"
    artifact_root.mkdir()
    monkeypatch.chdir(artifact_root)

    settings = _settings_from_bridge_root(str(bridge_root))

    assert Path.cwd() == artifact_root
    assert settings.run_dir == bridge_root / ".runtime"


def test_repo_send_launchers_select_current_route() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    shell = (repo_root / "scripts" / "imcodex-send").read_text(encoding="utf-8")
    windows = (repo_root / "scripts" / "imcodex-send.cmd").read_text(
        encoding="utf-8"
    )

    assert "--current" in shell
    assert '--bridge-root "${repo_root}"' in shell
    assert '"$@"' in shell
    assert "--current" in windows
    assert '--bridge-root "%REPO_ROOT%"' in windows
    assert "if defined IMCODEX_PYTHON" in windows
    assert 'else if exist "%REPO_ROOT%\\.venv\\Scripts\\python.exe"' in windows
    assert "else if defined CONDA_PREFIX" in windows
    assert "where python" in windows
    assert "%*" in windows


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_windows_send_launcher_prefers_configured_imcodex_python(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    launcher = scripts_dir / "imcodex-send.cmd"
    shutil.copy2(source_root / "scripts" / "imcodex-send.cmd", launcher)
    capture_path = tmp_path / "python-arguments.txt"
    fake_python = tmp_path / "configured-python.cmd"
    fake_python.write_text(
        "@echo off\n"
        "> \"%IMCODEX_TEST_CAPTURE%\" echo %*\n"
        "exit /b 0\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["IMCODEX_PYTHON"] = str(fake_python)
    environment["IMCODEX_TEST_CAPTURE"] = str(capture_path)
    environment.pop("CONDA_PREFIX", None)

    completed = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher), "--text", "done"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    arguments = capture_path.read_text(encoding="utf-8").strip()
    assert arguments.startswith("-m imcodex channels send --current --bridge-root")
    assert arguments.endswith("--text done")

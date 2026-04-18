from __future__ import annotations

from pathlib import Path

from imcodex.config import Settings


def test_settings_reads_optional_app_server_url_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_APP_SERVER_URL", "ws://127.0.0.1:8765")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.app_server_url == "ws://127.0.0.1:8765"


def test_settings_reads_optional_run_dir_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_RUN_DIR", ".custom-run")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.run_dir == Path(".custom-run")

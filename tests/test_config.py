from __future__ import annotations

from pathlib import Path

from imcodex.config import Settings


def test_settings_reads_optional_app_server_url_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_APP_SERVER_URL", "ws://127.0.0.1:8765")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.app_server_url == "ws://127.0.0.1:8765"


def test_settings_disables_app_server_experimental_api_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.app_server_experimental_api_enabled is False


def test_settings_reads_app_server_experimental_api_flag_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_APP_SERVER_EXPERIMENTAL_API", "1")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.app_server_experimental_api_enabled is True


def test_settings_reads_optional_run_dir_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_RUN_DIR", ".custom-run")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.run_dir == Path(".custom-run")


def test_settings_reads_optional_debug_api_flag_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_DEBUG_API_ENABLED", "1")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.debug_api_enabled is True


def test_settings_enables_qq_markdown_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.qq_markdown_enabled is True
    assert settings.channel_configs()["qq"]["markdown_enabled"] is True


def test_settings_reads_optional_qq_markdown_flag_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_QQ_MARKDOWN_ENABLED", "0")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.qq_markdown_enabled is False
    assert settings.channel_configs()["qq"]["markdown_enabled"] is False


def test_settings_reads_qq_access_allowlists(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_QQ_ALLOWED_USER_IDS", "owner,backup")
    monkeypatch.setenv("IMCODEX_QQ_ALLOWED_CONVERSATION_IDS", "c2c:owner")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    config = settings.channel_configs()["qq"]
    assert config["allowed_user_ids"] == "owner,backup"
    assert config["allowed_conversation_ids"] == "c2c:owner"


def test_settings_reads_core_mode_and_restart_executor(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_CORE_MODE", "dedicated-ws")
    monkeypatch.setenv("IMCODEX_CORE_URL", "ws://127.0.0.1:9001")
    monkeypatch.setenv("IMCODEX_RESTART_EXECUTOR", "scripts/restart-imcodex.ps1")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.core_mode == "dedicated-ws"
    assert settings.core_url == "ws://127.0.0.1:9001"
    assert settings.restart_executor == "scripts/restart-imcodex.ps1"


def test_settings_reads_app_server_auth_and_retry_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_APP_SERVER_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("IMCODEX_APP_SERVER_AUTH_TOKEN_FILE", "token.txt")
    monkeypatch.setenv("IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY", "0.1")
    monkeypatch.setenv("IMCODEX_APP_SERVER_RETRY_MAX_DELAY", "3.5")
    monkeypatch.setenv("IMCODEX_APP_SERVER_RETRY_JITTER", "0.2")
    monkeypatch.setenv("IMCODEX_APP_SERVER_CONNECT_TIMEOUT", "1.25")
    monkeypatch.setenv("IMCODEX_APP_SERVER_HEALTH_TIMEOUT", "0.75")

    settings = Settings.from_env()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert settings.app_server_auth_token == "secret-token"
    assert settings.app_server_auth_token_file == Path("token.txt")
    assert settings.app_server_connect_max_attempts == 4
    assert settings.app_server_request_max_attempts == 5
    assert settings.app_server_retry_initial_delay_s == 0.1
    assert settings.app_server_retry_max_delay_s == 3.5
    assert settings.app_server_retry_jitter_fraction == 0.2
    assert settings.app_server_connect_timeout_s == 1.25
    assert settings.app_server_health_timeout_s == 0.75

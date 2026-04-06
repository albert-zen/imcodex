from pathlib import Path

from imcodex.config import Settings


def test_settings_from_env_reads_dotenv_file(tmp_path: Path, monkeypatch) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "IMCODEX_QQ_ENABLED=1",
                "IMCODEX_QQ_APP_ID=1903391685",
                "IMCODEX_QQ_CLIENT_SECRET=test-secret",
                "IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com",
                "IMCODEX_DATA_DIR=.imcodex-data",
                "IMCODEX_HTTP_HOST=127.0.0.1",
                "IMCODEX_HTTP_PORT=9000",
                "IMCODEX_AUTO_APPROVE=1",
                "IMCODEX_AUTO_APPROVE_MODE=session",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMCODEX_QQ_ENABLED", raising=False)
    monkeypatch.delenv("IMCODEX_QQ_APP_ID", raising=False)
    monkeypatch.delenv("IMCODEX_QQ_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("IMCODEX_QQ_API_BASE", raising=False)
    monkeypatch.delenv("IMCODEX_DATA_DIR", raising=False)
    monkeypatch.delenv("IMCODEX_HTTP_HOST", raising=False)
    monkeypatch.delenv("IMCODEX_HTTP_PORT", raising=False)
    monkeypatch.delenv("IMCODEX_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("IMCODEX_AUTO_APPROVE_MODE", raising=False)

    settings = Settings.from_env()

    assert settings.qq_enabled is True
    assert settings.qq_app_id == "1903391685"
    assert settings.qq_client_secret == "test-secret"
    assert settings.qq_api_base == "https://sandbox.api.sgroup.qq.com"
    assert settings.data_dir == Path(".imcodex-data")
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 9000
    assert settings.auto_approve is True
    assert settings.auto_approve_mode == "session"

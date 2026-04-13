from __future__ import annotations

from imcodex.config import Settings
from imcodex.main import run


def test_run_uses_http_host_port_and_passes_settings(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".imcodex-data",
        codex_bin="codex",
        http_host="127.0.0.1",
        http_port=9100,
        app_server_host="127.0.0.1",
        app_server_port=8765,
        outbound_url=None,
        service_name="imcodex",
        default_permission_profile="review",
        qq_enabled=False,
        qq_app_id="",
        qq_client_secret="",
        qq_api_base="https://sandbox.api.sgroup.qq.com",
    )
    captured: dict[str, object] = {}

    def fake_create_application(*, settings=None, runtime=None):
        captured["settings"] = settings
        captured["runtime"] = runtime
        return "app"

    def fake_uvicorn_run(app, *, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("imcodex.main.Settings.from_env", lambda: settings)
    monkeypatch.setattr("imcodex.main.create_application", fake_create_application)
    monkeypatch.setattr("imcodex.main.uvicorn.run", fake_uvicorn_run)

    run()

    assert captured == {
        "settings": settings,
        "runtime": None,
        "app": "app",
        "host": "127.0.0.1",
        "port": 9100,
    }

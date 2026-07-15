from __future__ import annotations

from types import SimpleNamespace

from imcodex.main import run


def test_main_exposes_uvicorn_graceful_shutdown_callback(monkeypatch) -> None:
    settings = SimpleNamespace(http_host="127.0.0.1", http_port=8123)
    app = SimpleNamespace(state=SimpleNamespace())
    observed: dict[str, object] = {}

    class _Server:
        should_exit = False

        def __init__(self, config) -> None:
            observed["config"] = config
            observed["server"] = self

        def run(self) -> None:
            observed["ran"] = True

    def create_application(**kwargs):
        observed["application"] = kwargs
        return app

    monkeypatch.setattr(
        "imcodex.main.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        "imcodex.main.create_application",
        create_application,
    )
    monkeypatch.setattr(
        "imcodex.main.preflight_runtime_configuration",
        lambda resolved_settings: observed.setdefault("preflight", resolved_settings),
    )
    monkeypatch.setattr(
        "imcodex.main.uvicorn.Config",
        lambda resolved_app, **kwargs: (resolved_app, kwargs),
    )
    monkeypatch.setattr("imcodex.main.uvicorn.Server", _Server)

    assert run([]) == 0
    app.state.request_shutdown()

    assert observed["application"] == {
        "settings": settings,
        "settings_source": "environment",
    }
    assert observed["preflight"] is settings
    assert observed["config"] == (
        app,
        {"host": "127.0.0.1", "port": 8123},
    )
    assert observed["ran"] is True
    assert observed["server"].should_exit is True

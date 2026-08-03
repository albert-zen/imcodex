from __future__ import annotations

import runpy
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


def test_module_entrypoint_does_not_restart_inside_spawned_child(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("imcodex.main.run", lambda: calls.append("run"))

    runpy.run_module("imcodex", run_name="__mp_main__")

    assert calls == []


def test_main_returns_nonzero_after_lifespan_failure(monkeypatch) -> None:
    settings = SimpleNamespace(http_host="127.0.0.1", http_port=8123)
    app = SimpleNamespace(state=SimpleNamespace())

    class _Server:
        should_exit = False
        lifespan = SimpleNamespace(startup_failed=False, shutdown_failed=False)

        def __init__(self, _config) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(
        "imcodex.main.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr("imcodex.main.create_application", lambda **_kwargs: app)
    monkeypatch.setattr(
        "imcodex.main.preflight_runtime_configuration",
        lambda _settings: None,
    )
    monkeypatch.setattr("imcodex.main.uvicorn.Config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("imcodex.main.uvicorn.Server", _Server)

    for startup_failed, shutdown_failed in ((True, False), (False, True)):
        _Server.lifespan = SimpleNamespace(
            startup_failed=startup_failed,
            shutdown_failed=shutdown_failed,
        )
        assert run([]) == 1

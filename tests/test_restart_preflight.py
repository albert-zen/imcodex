from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from imcodex.composition import (
    _addresses_can_bind,
    preflight_runtime_configuration,
    run_runtime_preflight,
)


def _settings(**overrides):
    values = {
        "http_host": "192.0.2.20",
        "http_port": 8123,
        "app_server_target": SimpleNamespace(transport="tcp-websocket"),
        "codex_bin": "codex",
        "app_server_auth_token": None,
        "app_server_auth_token_file": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_preflight_validates_bind_host_and_every_enabled_channel(monkeypatch) -> None:
    calls: list[object] = []
    settings = _settings()

    class _Channel:
        def __init__(self, channel_id: str) -> None:
            self.channel_id = channel_id

        def validate_startup_configuration(self) -> None:
            calls.append(("channel", self.channel_id))

    def getaddrinfo(host, port, *, type, **_kwargs):
        calls.append(("resolve", host, port, type))
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]

    def build_runtime(resolved_settings, *, settings_source):
        calls.append(("runtime", resolved_settings, settings_source))
        return SimpleNamespace(managed_channels=[_Channel("qq"), _Channel("weixin")])

    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr("imcodex.composition.socket.getaddrinfo", getaddrinfo)
    monkeypatch.setattr("imcodex.composition.build_runtime", build_runtime)

    preflight_runtime_configuration()

    assert calls[0][0:3] == ("resolve", "192.0.2.20", 8123)
    assert calls[1] == ("runtime", settings, "environment")
    assert calls[2:] == [("channel", "qq"), ("channel", "weixin")]


def test_preflight_rejects_missing_stdio_codex_executable_before_runtime_build(
    monkeypatch,
) -> None:
    settings = _settings(
        app_server_target=SimpleNamespace(transport="stdio-jsonl"),
        codex_bin="missing-codex",
    )
    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        "imcodex.composition.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))
        ],
    )
    monkeypatch.setattr("imcodex.composition.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "imcodex.composition.build_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must not be built"),
    )

    with pytest.raises(RuntimeError, match="Codex executable was not found"):
        preflight_runtime_configuration()


def test_preflight_rejects_unresolvable_http_bind_host(monkeypatch) -> None:
    settings = _settings(http_host="not-a-real-bind-host.invalid")
    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(lambda _cls: settings),
    )

    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr("imcodex.composition.socket.getaddrinfo", fail_resolution)

    with pytest.raises(ValueError, match="could not be resolved"):
        preflight_runtime_configuration()


def test_preflight_rejects_an_occupied_replacement_port(monkeypatch) -> None:
    settings = _settings(http_host="127.0.0.1", http_port=8123)
    monkeypatch.setenv("IMCODEX_INTERNAL_PREFLIGHT_CURRENT_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("IMCODEX_INTERNAL_PREFLIGHT_CURRENT_HTTP_PORT", "8000")
    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        "imcodex.composition.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))
        ],
    )
    monkeypatch.setattr(
        "imcodex.composition._addresses_can_bind",
        lambda _addresses, *, port: port == 0,
    )

    with pytest.raises(ValueError, match="not available"):
        preflight_runtime_configuration()


def test_preflight_rejects_a_resolvable_nonlocal_bind_address(monkeypatch) -> None:
    settings = _settings(http_host="192.0.2.20")
    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        "imcodex.composition.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.20", port))
        ],
    )
    monkeypatch.setattr(
        "imcodex.composition._addresses_can_bind",
        lambda _addresses, *, port: False,
    )

    with pytest.raises(ValueError, match="local bindable"):
        preflight_runtime_configuration()


def test_bind_probe_fails_when_any_resolved_listener_is_occupied(monkeypatch) -> None:
    class _Probe:
        def __init__(self, family: int) -> None:
            self.family = family

        def setsockopt(self, *_args) -> None:
            return None

        def bind(self, _target) -> None:
            if self.family == socket.AF_INET:
                raise OSError("IPv4 listener is occupied")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "imcodex.composition.socket.socket",
        lambda family, _socktype, _protocol: _Probe(family),
    )
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 8123)),
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 8123, 0, 0)),
    ]

    assert _addresses_can_bind(addresses, port=8123) is False


def test_preflight_diagnostic_redacts_raw_settings_parse_value(monkeypatch, capsys) -> None:
    def invalid_settings(_cls):
        raise ValueError("sensitive-invalid-value")

    monkeypatch.setattr(
        "imcodex.composition.Settings.from_env",
        classmethod(invalid_settings),
    )

    assert run_runtime_preflight() == 1

    diagnostic = capsys.readouterr().err
    assert "settings could not be parsed (ValueError)" in diagnostic
    assert "sensitive-invalid-value" not in diagnostic

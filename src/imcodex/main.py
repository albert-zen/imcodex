from __future__ import annotations

import sys

import uvicorn

from .application import create_application
from .config import Settings
from .composition import preflight_runtime_configuration


def run(argv: list[str] | None = None) -> int | None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"debug", "core", "app-server"}:
        raise SystemExit(
            f"{argv[0]} is no longer an IMCodex command; Codex App Server lifecycle "
            "is owned by the public IM Agent SDK"
        )
    if argv and argv[0] == "ops":
        from .ops_cli import run_ops_cli

        return run_ops_cli(argv[1:])
    if argv and argv[0] == "channels":
        from .channels_cli import run_channels_cli

        return run_channels_cli(argv[1:])
    settings = Settings.from_env()
    preflight_runtime_configuration(settings)
    app = create_application(settings=settings, settings_source="environment")
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.http_host,
            port=settings.http_port,
        )
    )
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

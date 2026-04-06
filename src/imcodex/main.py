from __future__ import annotations

import uvicorn

from .application import create_application
from .config import Settings


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_application(settings=settings),
        host=settings.http_host,
        port=settings.http_port,
    )


if __name__ == "__main__":
    run()

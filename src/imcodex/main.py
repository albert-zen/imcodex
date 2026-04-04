from __future__ import annotations

import uvicorn

from .application import create_application


def run() -> None:
    uvicorn.run(create_application(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()

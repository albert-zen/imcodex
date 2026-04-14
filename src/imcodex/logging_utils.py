from __future__ import annotations

import logging
import textwrap


def configure_logging(level: str = "INFO") -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    if not getattr(configure_logging, "_configured", False):
        logging.basicConfig(
            level=resolved_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        configure_logging._configured = True
    root.setLevel(resolved_level)


def summarize_text(text: str | None, *, width: int = 160) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= width:
        return collapsed
    return textwrap.shorten(collapsed, width=width, placeholder="...")

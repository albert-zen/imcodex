from __future__ import annotations

import logging
import textwrap


SENSITIVE_TRANSPORT_LOGGERS = (
    "Lark",
    "httpcore",
    "httpx",
    "websockets",
)


def harden_transport_logging() -> None:
    """Keep credentials and wire payloads out of dependency logs.

    Telegram embeds the bot token in its request URL, and websocket DEBUG
    records may include authentication frames.  imcodex emits its own
    redacted transport events, so these dependency loggers stay at WARNING
    even when the bridge itself is running at DEBUG.
    """

    for logger_name in SENSITIVE_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


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
    harden_transport_logging()


def summarize_text(text: str | None, *, width: int = 160) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= width:
        return collapsed
    return textwrap.shorten(collapsed, width=width, placeholder="...")

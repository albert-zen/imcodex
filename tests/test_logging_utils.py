from __future__ import annotations

import io
import logging

from imcodex import logging_utils


def test_configure_logging_sets_root_level_with_preconfigured_handlers() -> None:
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    had_configured_flag = hasattr(logging_utils.configure_logging, "_configured")
    previous_configured_flag = getattr(logging_utils.configure_logging, "_configured", None)
    root.handlers[:] = [logging.StreamHandler(io.StringIO())]
    root.setLevel(logging.WARNING)
    if had_configured_flag:
        delattr(logging_utils.configure_logging, "_configured")

    try:
        logging_utils.configure_logging("DEBUG")
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
        if had_configured_flag:
            setattr(logging_utils.configure_logging, "_configured", previous_configured_flag)
        elif hasattr(logging_utils.configure_logging, "_configured"):
            delattr(logging_utils.configure_logging, "_configured")

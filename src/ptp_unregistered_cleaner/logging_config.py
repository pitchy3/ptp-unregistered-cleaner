"""Logging setup."""

from __future__ import annotations

import logging
import os
import sys


def configure_logging(level: str | None = None) -> None:
    selected = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, selected, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # HTTP 200 request lines are diagnostics, not per-run operational events.
    # Keep them accessible with LOG_LEVEL=DEBUG.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if selected == "DEBUG" else logging.WARNING
    )

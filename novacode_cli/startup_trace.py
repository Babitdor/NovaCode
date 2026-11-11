"""Lightweight startup phase tracing for boot-time diagnostics.

The first user-visible line of a Nova launch is a bare ``· nova: loading…`` and
the first status the TUI renders is the animated boot sequence — the gap in
between is where a cold start can stall without any feedback. This module
records the phase boundaries of the startup path so the tail of
``~/.nova/logs/startup.log`` shows exactly where a launch stopped.

Kept deliberately tiny: a timestamped line per phase, no subscribers, no state.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

_LOGGER_NAME = "novacode_cli.startup"

#: Where the phase lines land; module-level so tests can point it at a tmp file.
_LOG_PATH = Path.home() / ".nova" / "logs" / "startup.log"

_logger = logging.getLogger(_LOGGER_NAME)
_START_MONOTONIC = time.monotonic()


def _ensure_handler() -> None:
    """Attach a small rotating file handler to the phase logger, once."""
    if _logger.handlers:
        return
    from logging.handlers import RotatingFileHandler

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _LOG_PATH, mode="a", encoding="utf-8", maxBytes=64_000, backupCount=1, delay=True
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def mark(phase: str) -> None:
    """Record a startup phase boundary and how long the boot has run."""
    _ensure_handler()
    _logger.info(
        "startup phase: %s (boot_elapsed=%.3fs)", phase, time.monotonic() - _START_MONOTONIC
    )

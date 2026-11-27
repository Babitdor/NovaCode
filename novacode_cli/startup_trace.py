"""Lightweight startup phase tracing for boot-time diagnostics.

This module records the phase boundaries behind the startup animation so the tail of
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

    class _TraceHandler(RotatingFileHandler):
        """A rotating handler that never talks back.

        Nova processes overlap — the TUI, Desktop pipe sessions, one-shot
        commands all hold ``startup.log`` open at once — so a rollover's
        rename fails with ``WinError 32`` whenever another session is live.
        ``handleError`` would print the whole traceback to the user's stderr
        (it runs outside :func:`mark`'s guard, inside the logging machinery);
        the trace is diagnostic, so a record lost to a failed rotation is
        silently dropped and the log simply grows past its cap until the
        other sessions exit.
        """

        def handleError(self, record: logging.LogRecord) -> None:
            pass

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = _TraceHandler(
        _LOG_PATH, mode="a", encoding="utf-8", maxBytes=64_000, backupCount=1, delay=True
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def mark(phase: str) -> None:
    """Record a startup phase boundary and how long the boot has run."""
    try:
        _ensure_handler()
        _logger.info(
            "startup phase: %s (boot_elapsed=%.3fs)", phase, time.monotonic() - _START_MONOTONIC
        )
    except OSError:
        # Diagnostics must not prevent launching in a read-only or locked home.
        pass

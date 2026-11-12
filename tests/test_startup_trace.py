"""Phase tracing for the startup path — boot boundaries land in startup.log."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from novacode_cli import startup_trace

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_mark_writes_a_phase_line_to_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Importing main during collection may already have installed a handler.
    # Keep this test independent of that process-wide startup logger.
    logger = logging.getLogger("novacode_cli.startup.test")
    monkeypatch.setattr(startup_trace, "_logger", logger)
    monkeypatch.setattr(startup_trace, "_LOG_PATH", tmp_path / "startup.log")
    try:
        startup_trace.mark("session_start")
        startup_trace.mark("model_ready")

        lines = (tmp_path / "startup.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "startup phase: session_start" in lines[0]
        assert "startup phase: model_ready" in lines[1]
        assert "startup phase:" in lines[0]
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_unwritable_diagnostic_log_does_not_abort_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> None:
        message = "log directory unavailable"
        raise PermissionError(message)

    monkeypatch.setattr(startup_trace, "_ensure_handler", unavailable)
    startup_trace.mark("heavy_imports_start")

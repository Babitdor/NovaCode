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


def test_a_failed_rollover_drops_the_record_without_printing_to_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nova processes hold startup.log open concurrently, so the rollover's
    rename fails with WinError 32 while another session is live. The logging
    machinery prints that traceback to stderr itself — outside mark()'s
    guard — so the handler must swallow it instead: the trace is diagnostic
    and a record lost to a failed rotation is acceptable noise."""
    import os

    logger = logging.getLogger("novacode_cli.startup.test.rollover")
    monkeypatch.setattr(startup_trace, "_logger", logger)
    monkeypatch.setattr(startup_trace, "_LOG_PATH", tmp_path / "startup.log")

    def refusing_rename(source, dest, **_kwargs: object) -> None:
        raise PermissionError(32, "file is open in another process")

    monkeypatch.setattr(os, "rename", refusing_rename)
    try:
        # ~1 200 short records cross the handler's 64 KB cap, forcing rollovers
        # that the refusing rename turns into the exact production failure.
        for index in range(1200):
            startup_trace.mark(f"phase-{index}")

        captured = capsys.readouterr()
        assert "Logging error" not in captured.err
        assert "PermissionError" not in captured.err
        assert "Traceback" not in captured.err
        # Rotation was impossible, so nothing was moved away: the file keeps
        # its pre-cap content and freezes there (records past the cap are
        # dropped until some later process's rotation finally succeeds).
        text = (tmp_path / "startup.log").read_text(encoding="utf-8")
        assert "startup phase: phase-0" in text
        assert "startup phase:" in text.splitlines()[-1]
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

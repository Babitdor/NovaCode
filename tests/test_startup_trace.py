"""Phase tracing for the startup path — boot boundaries land in startup.log."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli import startup_trace

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_mark_writes_a_phase_line_to_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(startup_trace, "_LOG_PATH", tmp_path / "startup.log")
    startup_trace.mark("session_start")
    startup_trace.mark("model_ready")

    lines = (tmp_path / "startup.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "startup phase: session_start" in lines[0]
    assert "startup phase: model_ready" in lines[1]
    assert "startup phase:" in lines[0]

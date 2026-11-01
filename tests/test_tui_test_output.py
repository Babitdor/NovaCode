"""Native test output uses one bounded display rather than one widget per line."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from novacode_cli.tui.widgets import OutputLog
from tests.test_tui_subagent_panel import _app, _isolate_panel_tests  # noqa: F401


@pytest.mark.timeout(30)
async def test_test_command_batches_output_and_keeps_recent_lines(monkeypatch):
    async def run_tests(**kwargs):
        callback = kwargs["output_callback"]
        for n in range(5000):
            callback(f"test-{n} passed")
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr("novacode_cli.server_runner.test_runner.run_tests", run_tests)
    app = _app()
    reports = []
    async with app.run_test(size=(100, 35)) as pilot:
        app._log = lambda *args, **kwargs: reports.append(args)
        await app._run_tests("/tests pytest")
        output = app.query_one(".test-run-output", OutputLog)
        async with asyncio.timeout(5):
            while "test-4999 passed" not in output._output_body.content.plain:
                await pilot.pause(0.05)
        assert len(reports) < 5
        assert len(output.lines) <= output.max_lines
        assert "test-4999 passed" in output._output_body.content.plain
        assert not app._turn_active

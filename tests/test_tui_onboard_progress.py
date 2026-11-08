"""Onboarding stays visibly active without retaining transient chat rows."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from novacode_cli.tui import motion
from tests.test_tui_app import _disable_live_update_checks, _wait_until  # noqa: F401
from tests.test_tui_compaction_row import _app


@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
async def test_inline_onboarding_animates_and_always_cleans_up(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    started, release = asyncio.Event(), asyncio.Event()

    async def command(text: str) -> None:
        assert text == "/onboard refresh"
        started.set()
        await release.wait()
        if outcome == "error":
            msg = "Synthetic command failure"
            raise ValueError(msg)

    monkeypatch.setattr(app, "_passthrough_command", command)
    async with app.run_test(size=(110, 40)) as pilot:
        task = asyncio.create_task(app._run_onboard("/onboard refresh"))
        try:
            await started.wait()
            await pilot.pause()
            row = app.query_one(".nova-onboard-progress", Static)
            first = str(row.render())
            assert "Onboarding project" in first
            assert first[0] in motion.SPINNER_FRAMES
            assert row.region.height > 0
            assert not app._home_visible
            assert await _wait_until(pilot, lambda: str(row.render()) != first)
            assert "s" in str(row.render())

            if outcome == "cancel":
                task.cancel()
            else:
                release.set()
            if outcome == "complete":
                await task
            else:
                expected: type[BaseException] = (
                    asyncio.CancelledError if outcome == "cancel" else ValueError
                )
                with pytest.raises(expected):
                    await task
            await pilot.pause()
            assert not app.query(".nova-onboard-progress")
            assert app._activity == "ready"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

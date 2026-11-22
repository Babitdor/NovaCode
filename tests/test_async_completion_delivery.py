"""Async results that arrive mid-turn are fetched after the turn settles."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


async def test_completion_during_turn_is_queued_and_reported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    reported: list[str] = []
    monkeypatch.setattr(app, "_auto_report_async_done", reported.append)

    async with app.run_test() as pilot:
        app._turn_active = True
        message = "review done\n[async_task_id=review-123]"
        app._notify_async_done("success", "Async agent completed", message)
        app._notify_async_done("success", "Async agent completed", message)
        await pilot.pause(0.1)
        assert reported == []
        assert app._pending_async_completion_ids == ["review-123"]

        app._turn_active = False
        app._schedule_pending_async_report()
        await pilot.pause(0.1)
        assert reported == ["review-123"]
        assert app._pending_async_completion_ids == []


async def test_a_cancelled_auto_report_requeues_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exclusive-group cancel must not lose the popped completion id.

    Regression: _dispatch_pending_async_report popped the id before the report
    turn started; a second completion landing in the pop→_turn_active window
    cancelled the first report (exclusive group) and the id was gone — that
    task's result was never reported.
    """

    app = _app()

    async def cancelled(task_id: str) -> None:  # noqa: ANN001
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "_report_async_result", cancelled)
    async with app.run_test() as pilot:
        app._auto_report_async_done("review-123")
        await pilot.pause(0.1)
        assert app._pending_async_completion_ids == ["review-123"]

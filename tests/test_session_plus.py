"""The tab bar starts blank conversations without a naming dialog."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Tabs

from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.timeout(30)
async def test_plus_creates_blank_session_and_preserves_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.sessions import worktree
    from novacode_cli.tui.widgets import MatrixRain, PromptInput

    monkeypatch.setattr(
        worktree,
        "create_worktree",
        lambda *_args, **_kwargs: SimpleNamespace(path=tmp_path, branch=None, warnings=[]),
    )
    app = _app()
    supervisor = SimpleNamespace(
        at_capacity=lambda: False,
        spawn=AsyncMock(return_value=object()),
        send_prompt=AsyncMock(return_value="p1"),
    )
    app._session_supervisor = supervisor
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        root = app._root_pane
        rain = app._home_banner
        app._set_home_visible(visible=False)
        await pilot.click("#new-session")
        await app.workers.wait_for_complete()
        await pilot.pause()
        child = app._active_pane
        assert child is not root
        assert app.query_one("#session-tabs", Tabs).active == child.sid
        assert app._home_visible
        assert app._home_banner is rain
        assert len(app.query(MatrixRain)) == 1
        assert app.query_one("#prompt", PromptInput).has_focus
        supervisor.spawn.assert_awaited_once()
        supervisor.send_prompt.assert_not_awaited()
        await app._switch_to(root)
        assert not app._home_visible
        await app._switch_to(child)
        assert app._home_visible
        await app._dispatch_to_child(child, "hello")
        assert not app._home_visible
        await app._switch_to(root)
        await app._switch_to(child)
        assert not app._home_visible


async def test_plus_is_visible_for_first_session_and_capacity_safe():
    app = _app()
    app._session_supervisor = SimpleNamespace(at_capacity=lambda: True)
    async with app.run_test(size=(48, 18)) as pilot:
        await pilot.pause()
        button = app.query_one("#new-session", Button)
        assert button.visible
        assert button.region.right <= app.size.width
        await pilot.click(button)
        await app.workers.wait_for_complete()
        assert len(app._panes) == 1
        assert app._active_pane is app._root_pane


@pytest.mark.timeout(30)
@pytest.mark.parametrize("active", [False, True])
async def test_cross_closes_its_session_without_selecting_other_tabs(*, active: bool):
    from novacode_cli.tui.session_tab import SessionTab

    app = _app()
    supervisor = SimpleNamespace(close=AsyncMock(return_value=None))
    app._session_supervisor = supervisor
    async with app.run_test(size=(120, 42)) as pilot:
        child = await _add_pane(app)
        if active:
            await app._switch_to(child)
        app._refresh_tabs()
        await pilot.pause()
        root_tab = app.query_one(f"#{app._root_pane.sid}", SessionTab)
        tab = app.query_one(f"#{child.sid}", SessionTab)
        assert not root_tab.closeable
        assert not root_tab.label.plain.endswith("\N{MULTIPLICATION SIGN}")
        assert tab.label.plain.endswith(" \N{MULTIPLICATION SIGN}")
        # The native tab's final label cell is the close target.
        await pilot.click(tab, offset=(tab.size.width - 2, 0))
        await app.workers.wait_for_complete()
        await pilot.pause()
        supervisor.close.assert_awaited_once_with(child.sid)
        assert child not in app._panes
        assert app._active_pane is app._root_pane
        assert not app.query(f"#{child.sid}")


@pytest.mark.timeout(30)
async def test_clicking_tab_label_still_selects_session():
    app = _app()
    async with app.run_test(size=(120, 42)) as pilot:
        child = await _add_pane(app)
        app._refresh_tabs()
        await pilot.pause()
        await pilot.click(f"#{child.sid}", offset=(2, 0))
        await app.workers.wait_for_complete()
        assert app._active_pane is child


@pytest.mark.timeout(30)
async def test_cross_disappears_before_slow_worker_shutdown():
    app = _app()
    closing = asyncio.Event()
    finish = asyncio.Event()

    async def close(_sid: str) -> None:
        closing.set()
        await finish.wait()

    app._session_supervisor = SimpleNamespace(close=AsyncMock(side_effect=close))
    async with app.run_test(size=(120, 42)) as pilot:
        child = await _add_pane(app)
        await app._switch_to(child)
        await pilot.pause()
        tab = app.query_one(f"#{child.sid}")
        await pilot.click(tab, offset=(tab.size.width - 2, 0))
        await asyncio.wait_for(closing.wait(), timeout=2)
        try:
            await pilot.pause()
            assert not app.query(f"#{child.sid}"), "the tab waited for process shutdown"
            assert app._active_pane is app._root_pane
        finally:
            finish.set()
            await app.workers.wait_for_complete()


@pytest.mark.timeout(30)
async def test_failed_shutdown_restores_tab():
    app = _app()
    app._session_supervisor = SimpleNamespace(close=AsyncMock(side_effect=OSError("stop failed")))
    async with app.run_test(size=(120, 42)) as pilot:
        child = await _add_pane(app)
        await app._switch_to(child)
        await app._close_session(child)
        await pilot.pause()
        assert child in app._panes
        assert app.query(f"#{child.sid}")
        assert child.scroll.is_mounted
        await app._switch_to(child)
        assert app._active_pane is child


@pytest.mark.timeout(30)
async def test_close_waits_for_startup_after_hiding_tab():
    app = _app()
    finish = asyncio.Event()
    supervisor = SimpleNamespace(close=AsyncMock(return_value=None))
    app._session_supervisor = supervisor
    async with app.run_test(size=(120, 42)) as pilot:
        child = await _add_pane(app)
        child.start_task = asyncio.create_task(finish.wait())
        await app._switch_to(child)
        worker = app.run_worker(app._close_session(child))
        try:
            await pilot.pause()
            assert not app.query(f"#{child.sid}")
            assert app._active_pane is app._root_pane
            supervisor.close.assert_not_awaited()
        finally:
            finish.set()
            await worker.wait()
        supervisor.close.assert_awaited_once_with(child.sid)

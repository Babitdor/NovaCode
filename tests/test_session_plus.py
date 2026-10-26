"""The tab bar starts blank conversations without a naming dialog."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Tabs

from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401

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

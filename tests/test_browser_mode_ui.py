"""Browser mode changes the composer without changing the graph, draft, or transcript."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, SelectionList, Static

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.browser_settings_screen import BrowserSettingsScreen
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    from textual.worker import Worker


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(120, 42), (40, 24)])
async def test_toggle_changes_ui_preserves_draft_and_keeps_shared_conversation(
    size: tuple[int, int],
):
    app = _app()
    original_agent = app.agent
    original_thread = app.session_state.thread_id
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.text = "Keep my draft"
        marker = Static("Existing browser findings")
        await app._transcript().mount(marker)
        await app._run_browser_use("/browser-use")
        await pilot.pause()
        assert app.session_state.browser_mode_enabled
        assert app.screen.has_class("browsing-mode")
        assert prompt.has_class("browser-mode")
        assert "BROWSER MODE" in str(app.query_one("#mode-badge", Static).render())
        assert app.query_one("#browser-toggle", Button).has_class("enabled")
        assert app.query_one("#browser-toggle").region.width > 0
        assert app.query_one("#browser-toggle").region.height == 1
        assert app.query_one("#browser-toggle").region.bottom <= app.size.height
        assert app._input_pulse_mode == "browser"
        assert prompt.text == "Keep my draft"
        assert marker.is_mounted
        await pilot.click("#browser-toggle")
        await pilot.pause()
        assert not app.screen.has_class("browsing-mode")
        assert not prompt.has_class("browser-mode")
        assert prompt.text == "Keep my draft"
        assert marker.is_mounted
        assert app.agent is original_agent
        assert app.session_state.thread_id == original_thread


async def test_task_uses_current_agent_and_busy_toggle_cannot_relax_restrictions(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    stream = AsyncMock()
    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_browser_use("/browser-use Research Rust browsers")
        assert app.session_state.browser_mode_enabled
        stream.assert_awaited_once_with("Research Rust browsers")
        monkeypatch.setattr(app, "_turn_running", lambda: True)
        await app._run_browser_use("/browser-use off")
        assert app.session_state.browser_mode_enabled
        await app._run_browser_use("/browser-use settings")
        assert not isinstance(app.screen, BrowserSettingsScreen)
        await app._run_plan("/plan")
        assert app.session_state.browser_mode_enabled
        assert not app.session_state.plan_mode_enabled


async def test_browser_mode_rejects_direct_shell_shortcuts(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    shell = AsyncMock()
    monkeypatch.setattr(app, "_bg_shell_worker", shell)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_browser_use("/browser-use on")
        await app._run_bash("!echo should-not-run")
        shell.assert_not_called()


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(120, 42), (40, 24)])
async def test_browser_settings_save_cancel_and_empty_selection(
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    servers = {
        "obscura": SimpleNamespace(disabled=False),
        "my-playwright": SimpleNamespace(disabled=False),
        "devtools": SimpleNamespace(disabled=True),
    }
    monkeypatch.setattr(
        "novacode_cli.mcp.config.MCPConfig",
        lambda: SimpleNamespace(list_servers=lambda: servers),
    )
    app = _app()
    NovaConfig().set_browser_mode_servers(["obscura", "missing-browser"])
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one("#prompt").text = "Keep this research draft"

        async def open_settings() -> Worker[None]:
            worker = app.run_worker(app._run_browser_use("/browser-use settings"))
            async with asyncio.timeout(5):
                while not isinstance(app.screen, BrowserSettingsScreen):
                    await pilot.pause(0.01)
            await pilot.pause()
            return worker

        worker = await open_settings()
        assert isinstance(app.screen, BrowserSettingsScreen)
        choices = app.screen.query_one(SelectionList)
        assert set(choices.selected) == {"obscura", "missing-browser"}
        choices.select("my-playwright")
        choices.select("devtools")
        assert set(NovaConfig().get_browser_mode_servers()) == {"obscura", "missing-browser"}
        save = app.screen.query_one("#browser-settings-save")
        assert save.region.width > 0
        assert save.region.bottom <= app.size.height
        await pilot.click("#browser-settings-save")
        await worker.wait()
        assert set(NovaConfig().get_browser_mode_servers()) == {
            "obscura",
            "my-playwright",
            "devtools",
            "missing-browser",
        }
        assert servers["devtools"].disabled  # Selection never enables its connection.
        assert not app.session_state.browser_mode_enabled
        assert app.query_one("#prompt").text == "Keep this research draft"

        worker = await open_settings()
        app.screen.query_one(SelectionList).deselect_all()
        await pilot.press("escape")
        await worker.wait()
        assert "my-playwright" in NovaConfig().get_browser_mode_servers()

        worker = await open_settings()
        app.screen.query_one(SelectionList).deselect_all()
        await pilot.click("#browser-settings-save")
        await worker.wait()
        assert NovaConfig().get_browser_mode_servers() == []

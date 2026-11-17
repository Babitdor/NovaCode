"""Oracle composer controls preserve drafts and expose owner decisions and threads."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Switch

from novacode_cli import ui_events as ev
from novacode_cli.core.oracle import OracleRun, OracleSettings, Review, Reviewer
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.oracle_panel import OraclePanel, OracleSettingsScreen, OracleThreadScreen
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.fixture(autouse=True)
def minimal_mount(monkeypatch):
    async def mount(self):
        self._apply_saved_theme()
        self.query_one("#cmdpalette").display = False
        self._root_screen = self.screen
        self._init_root_pane()
        self._set_home_visible(visible=False)
        self._refresh_oracle()

    monkeypatch.setattr(NovaApp, "on_mount", mount)


@pytest.mark.parametrize("size", [(120, 42), (90, 36), (75, 32)])
@pytest.mark.parametrize("design", ["classic", "dashboard", "minimal"])
async def test_toggle_strip_settings_and_threads_preserve_draft(size, design):
    app = _app()
    async with app.run_test(size=size) as pilot:
        app.screen.add_class(f"{design}-ui")
        prompt = app.query_one("#prompt")
        prompt.text = "unfinished draft"
        await pilot.click("#oracle-toggle")
        await pilot.pause()
        assert app.session_state.oracle_enabled
        assert prompt.text == "unfinished draft"
        panel = app.query_one(OraclePanel)
        strip = panel.query_one("#oracle-strip", Button)
        assert strip.display
        assert app.screen.region.contains_region(strip.region)
        run = OracleRun(OracleSettings(), ".", "thread", round=2, status="waiting")
        run.reviewers = [
            Reviewer(
                "first",
                1,
                1,
                "first review input",
                Review(
                    score=5.0,
                    summary="First finding",
                    findings=["Fix code.py"],
                ),
            ),
            Reviewer(
                "final",
                2,
                1,
                "fresh review input",
                Review(
                    score=9.0,
                    summary="Fixed",
                    findings=[],
                ),
            ),
        ]
        run.decision = asyncio.get_running_loop().create_future()
        app.session_state.oracle_run = run
        app._refresh_oracle()
        await pilot.pause()
        assert panel.query_one("#oracle-actions").display
        await pilot.click("#oracle-strip")
        await pilot.pause()
        row = panel.query_one("#oracle-row-1")
        assert "first 5" in str(row.title) and "latest 9" in str(row.title)
        row.collapsed = False
        await pilot.pause()
        thread_button = panel.query_one("#oracle-thread-1", Button)
        thread_button.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click(thread_button)
        await pilot.pause()
        assert isinstance(app.screen, OracleThreadScreen)
        assert "fresh review input" in " ".join(str(w.content) for w in app.screen.query("Static"))
        await pilot.press("escape")
        await pilot.pause()
        await pilot.click("#oracle-accept")
        await pilot.pause()
        assert run.decision.result()["action"] == "accept"
        assert prompt.text == "unfinished draft"


async def test_settings_validate_cancel_and_save_without_sending(monkeypatch):
    from novacode_cli.config.nova_config import NovaConfig

    saved = []
    monkeypatch.setattr(NovaConfig, "set", lambda self, key, value: saved.append((key, value)))
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one("#prompt").text = "draft"
        await pilot.click("#oracle-settings")
        await pilot.pause()
        assert isinstance(app.screen, OracleSettingsScreen)
        app.screen.query_one("#oracle-target", Input).value = "11"
        save = app.screen.query_one("#oracle-settings-save", Button)
        save.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click(save)
        await pilot.pause()
        assert isinstance(app.screen, OracleSettingsScreen)
        assert not saved
        app.screen.query_one("#oracle-target", Input).value = "9"
        app.screen.query_one("#oracle-rounds", Input).value = "4"
        app.screen.query_one("#oracle-reviewers", Input).value = "3"
        app.screen.query_one("#oracle-default", Switch).value = True
        save.scroll_visible(immediate=True)
        await pilot.pause(0.25)
        await pilot.click(save)
        await pilot.pause()
        assert saved[-1] == (
            "oracle_settings",
            {
                "target": 9.0,
                "max_rounds": 4,
                "reviewers": 3,
                "default_enabled": True,
            },
        )
        assert app.session_state.oracle_settings.target == 9
        assert app.query_one("#prompt").text == "draft"
        await pilot.click("#oracle-settings")
        await pilot.pause()
        await pilot.press("escape")
        assert len(saved) == 1


async def test_live_staleness_and_oracle_interrupt_controls(monkeypatch):
    from novacode_cli.core import oracle

    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        await app._run_oracle_mode("/oracle-mode on")
        run = OracleRun(
            OracleSettings(),
            ".",
            app.session_state.thread_id,
            round=1,
            status="passed",
            revision="old",
        )
        app.session_state.oracle_run = run
        monkeypatch.setattr(oracle, "snapshot", lambda _: SimpleNamespace(revision="new"))
        await app._check_oracle_freshness()
        assert run.status == "stale"
        run.decision = asyncio.get_running_loop().create_future()
        event = ev.InterruptRequest("oracle", {"run_id": run.identity}, run.decision)
        handler = asyncio.create_task(app._handle_interrupt(event))
        await pilot.pause()
        await pilot.click("#oracle-pause")
        await asyncio.wait_for(handler, 2)
        assert event.future.result()["action"] == "pause"


def test_new_threads_and_clear_use_saved_default(monkeypatch):
    from novacode_cli.core import oracle
    from novacode_cli.states.Session import SessionState

    monkeypatch.setattr(oracle, "profile_settings", lambda: OracleSettings(default_enabled=True))
    session = SessionState()
    assert session.oracle_enabled
    session.oracle_enabled = False
    session.oracle_run = OracleRun(OracleSettings(), ".", session.thread_id)
    session.reset_conversation()
    assert session.oracle_enabled
    assert session.oracle_run is None

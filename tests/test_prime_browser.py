"""Saved Prime work is selected by name, with explicit actions and no ID entry."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, OptionList, Static

from novacode_cli.prime import PrimeRun
from novacode_cli.tui.prime_panel import PrimePanel
from tests.test_prime_ui import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app


@pytest.mark.timeout(30)
async def test_empty_browser_explains_what_to_do_and_disables_record_actions():
    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Browsing must not start work"))
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await app._run_prime("/prime tasks")
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        assert panel.display
        assert panel.query_one("#prime-records", OptionList).option_count == 0
        assert "No saved tasks" in str(panel.query_one("#prime-hint", Static).render())
        for selector in ("#prime-prepare", "#prime-inspect", "#prime-forget", "#prime-continue"):
            assert panel.query_one(selector, Button).disabled
        app.agent.astream.assert_not_called()


@pytest.mark.timeout(30)
async def test_task_selection_and_tabs_do_not_load_or_run_until_explicit_action():
    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Selection must not start work"))
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        first = PrimeRun(app.session_state, "Build portfolio")
        first.finish("cancelled")
        app.session_state.prime_report = None
        second = PrimeRun(app.session_state, "Improve navigation")
        second.finish("cancelled")
        await app._run_prime("/prime tasks")
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        rows = panel.query_one("#prime-records", OptionList)
        assert rows.option_count == 2
        rows.highlighted = 0
        rows.focus()
        await pilot.press("down")
        await pilot.pause()
        chosen_title = str(rows.get_option_at_index(rows.highlighted).prompt)
        await pilot.press("enter")
        await pilot.pause()
        assert panel.query_one("#prime-content-scroll").display
        assert app.session_state.prime_pending is None
        await pilot.click("#prime-tasks")
        await pilot.pause()
        assert rows.display
        assert str(rows.get_option_at_index(rows.highlighted).prompt) == chosen_title
        await pilot.click("#prime-prepare")
        await pilot.pause()
        loaded_goal = app.session_state.prime_pending["goal"]
        assert loaded_goal in chosen_title
        assert "Loaded:" in str(panel.query_one("#prime-hint", Static).render())
        await pilot.click("#prime-forget")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.session_state.prime_pending is None
        assert panel.query_one("#prime-continue", Button).disabled
        assert rows.option_count == 1
        app.agent.astream.assert_not_called()


@pytest.mark.timeout(30)
async def test_storage_failure_is_readable_and_cannot_enable_record_actions(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.tui import prime_panel

    def unavailable(_session: object, _kind: str, _identity: str = "") -> list:
        msg = "storage unavailable"
        raise OSError(msg)

    monkeypatch.setattr(prime_panel, "_saved_records", unavailable)
    app = _app()
    async with app.run_test(size=(55, 24)) as pilot:
        await pilot.pause()
        await app._run_prime("/prime tasks")
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        assert "storage unavailable" in str(panel.query_one("#prime-hint", Static).render())
        for selector in ("#prime-prepare", "#prime-inspect", "#prime-forget"):
            assert panel.query_one(selector, Button).disabled


@pytest.mark.timeout(30)
async def test_slow_previous_tab_cannot_replace_current_check_selection(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.tui import prime_panel

    app = _app()
    started, released = threading.Event(), threading.Event()
    original = prime_panel._saved_records

    def delayed(session: object, kind: str, identity: str = "") -> list:
        if kind == "tasks" and not started.is_set():
            started.set()
            if not released.wait(3):
                msg = "test read was not released"
                raise TimeoutError(msg)
        return original(session, kind, identity)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        run = PrimeRun(app.session_state, "Build portfolio")
        run.checkpoint("define", "Build", criteria=["Build passes"])
        run.define_check(purpose="Portfolio build", command="npm run build", files=[], criteria=[0])
        run.finish("cancelled")
        await app._run_prime("/prime tasks")
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        monkeypatch.setattr(prime_panel, "_saved_records", delayed)
        old = asyncio.create_task(panel.show_details(app.session_state, view="tasks"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await panel.show_details(app.session_state, view="checks")
            assert "Portfolio build" in str(
                panel.query_one("#prime-records", OptionList).get_option_at_index(0).prompt
            )
        finally:
            released.set()
            await old
        assert "Portfolio build" in str(
            panel.query_one("#prime-records", OptionList).get_option_at_index(0).prompt
        )
        assert panel.query_one("#prime-prepare", Button).disabled


@pytest.mark.timeout(30)
async def test_legacy_check_command_selects_exact_record_and_rejects_missing_id():
    app = _app()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        run = PrimeRun(app.session_state, "Build portfolio")
        run.checkpoint("define", "Build", criteria=["Build passes"])
        check = run.define_check(
            purpose="Requested build", command="npm run build", files=[], criteria=[0]
        )
        run.finish("cancelled")
        await app._run_prime("/prime checks " + check["id"])
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        assert "Requested build" in str(panel.query_one("#prime-details", Static).render())
        await app._run_prime("/prime checks missing-check")
        await pilot.pause()
        assert panel.selected_record() is None
        assert panel.query_one("#prime-inspect", Button).disabled
        assert not panel.query_one("#prime-content-scroll").display


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_select_check_by_name_inspect_and_remove_with_confirmation(size: tuple[int, int]):
    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Browsing must not start work"))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = PrimeRun(app.session_state, "Build portfolio")
        run.checkpoint("define", "Production build", criteria=["Build passes"])
        check = run.define_check(
            purpose="Portfolio production build",
            command="npm run build",
            files=[],
            criteria=[0],
        )
        run.finish("cancelled")
        await app._run_prime("/prime checks")
        await pilot.pause()
        panel = app.query_one("#prime-panel", PrimePanel)
        records = panel.query_one("#prime-records", OptionList)
        assert records.option_count, str(panel.query_one("#prime-hint", Static).render())
        assert "Portfolio production build" in str(records.get_option_at_index(0).prompt)
        assert check["id"] not in str(records.get_option_at_index(0).prompt)
        assert panel.query_one("#prime-prepare", Button).disabled
        assert str(panel.query_one("#prime-inspect", Button).label) == "Details"
        records.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "npm run build" in str(panel.query_one("#prime-details", Static).render())
        await pilot.click("#prime-forget")
        await pilot.pause()
        assert "Portfolio production build" in str(
            app.screen.query_one("#modal-body", Static).render()
        )
        assert check["id"] not in str(app.screen.query_one("#modal-body", Static).render())
        await pilot.press("n")
        await pilot.pause()
        assert run.memory.get("checks", check["id"]) is not None
        await pilot.click("#prime-forget")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert run.memory.get("checks", check["id"]) is None
        assert run.memory.get("tasks", run.report["id"]) is not None
        assert panel.query_one("#prime-records", OptionList).option_count == 0
        assert panel.query_one("#prime-forget", Button).disabled
        app.agent.astream.assert_not_called()

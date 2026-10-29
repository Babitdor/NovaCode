"""Prime controls use the existing prompt modes and do not start agent work."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Button, Static

from novacode_cli import ui_events as ev
from novacode_cli.prime import PrimeRun
from novacode_cli.tui.prime_panel import PrimePanel
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app


@pytest.mark.timeout(30)
async def test_prime_toggle_phase_and_existing_permission_badges():
    app = _app()
    app.agent.astream = AsyncMock(
        side_effect=AssertionError("Changing Prime must not invoke an agent")
    )
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        original_approval = app.session_state.auto_approve
        assert not app.session_state.prime_enabled
        await app._run_prime("/prime on")
        assert app.session_state.prime_enabled
        assert app.screen.has_class("prime-mode")
        assert app.query_one("#prime-toggle", Button).has_class("enabled")
        await app._run_prime("/prime\ton")
        assert app.session_state.prime_enabled
        assert app.session_state.auto_approve is original_approval
        badge = app.query_one("#mode-badge", Static)
        assert "PRIME Define" in str(badge.render())
        run = PrimeRun(app.session_state, "Fix login")
        run.checkpoint("define", "Define success", criteria=["Regression passes"])
        await app._render(ev.ContextMessage("Prime progress", event_type="nova_prime_progress"))
        assert "PRIME Investigate" in str(badge.render())
        app.session_state.plan_mode_enabled = True
        app._update_mode_badge()
        assert "PLAN MODE" in str(badge.render())
        assert "PRIME" in str(badge.render())
        app.session_state.plan_mode_enabled = False
        app.session_state.auto_approve = True
        app._update_mode_badge()
        assert "AUTO-APPROVE" in str(badge.render())
        assert "PRIME" in str(badge.render())
        await app._run_prime("/prime off")
        assert not app.session_state.prime_enabled
        assert not app.screen.has_class("prime-mode")
        assert app.session_state.auto_approve
        assert "PRIME" not in str(badge.render())
        app.agent.astream.assert_not_called()


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_prime_drawer_prepare_continue_and_narrow_navigation(  # noqa: PLR0915 - One UI lifecycle.
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    from textual.widgets import Input, OptionList

    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Inspect/prepare must not start work"))
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_root", dispatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await app._run_prime("/prime on")
        run = PrimeRun(app.session_state, "Fix CLI")
        run.checkpoint("define", "Reproduce", criteria=["CLI works"])
        run.finish("cancelled")
        app._update_mode_badge()
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Keep this draft"
        transcript_y = app._transcript().scroll_y
        log = Mock(wraps=app._log)
        monkeypatch.setattr(app, "_log", log)
        panel = app.query_one("#prime-panel", PrimePanel)
        panel.query_one("#prime-strip", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        drawer = panel.query_one("#prime-drawer")
        assert drawer.display
        assert drawer.region.height <= size[1] // 3
        assert not panel.query(Input), "Selecting saved work must not require entering an ID"
        records = panel.query_one("#prime-records", OptionList)
        assert records.option_count == 1
        assert "Fix CLI" in str(records.get_option_at_index(0).prompt)
        assert run.report["id"] not in str(records.get_option_at_index(0).prompt)
        for selector in (
            "#prime-prepare",
            "#prime-inspect",
            "#prime-forget",
            "#prime-continue",
            "#prime-close",
        ):
            control = panel.query_one(selector, Button)
            assert control.region.height == 1
            assert control.content_region.height == 1
        records.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "Fix CLI" in str(panel.query_one("#prime-details", Static).render())
        await pilot.click("#prime-prepare")
        await pilot.pause()
        assert app.session_state.prime_pending["id"] == run.report["id"]
        assert not panel.query_one("#prime-continue", Button).disabled
        assert prompt.text == "Keep this draft"
        assert app._transcript().scroll_y == transcript_y
        assert not any(call.kwargs.get("transcript") for call in log.call_args_list)
        app.agent.astream.assert_not_called()
        dispatch.assert_not_called()
        panel.query_one("#prime-continue", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        dispatch.assert_called_once()
        await pilot.click("#prime-close")
        assert not drawer.display
        assert prompt.text == "Keep this draft"
        assert panel.query_one("#prime-strip", Button).region.right <= size[0]


@pytest.mark.timeout(30)
async def test_prime_storage_warning_is_temporary_and_shown_once(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        notify = Mock(wraps=app.notify)
        log = Mock(wraps=app._log)
        monkeypatch.setattr(app, "notify", notify)
        monkeypatch.setattr(app, "_log", log)
        event = ev.ContextMessage(
            "Prime progress",
            event_type="nova_prime_progress",
            record_id="record",
            data={"storage_error": "Prime drafts cannot be saved"},
        )
        await app._render(event)
        await app._render(event)
        warnings = [
            call for call in notify.call_args_list if call.args[0] == "Prime drafts cannot be saved"
        ]
        assert len(warnings) == 1
        assert warnings[0].kwargs["severity"] == "warning"
        assert not any(call.kwargs.get("transcript") for call in log.call_args_list)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_composer_prime_toggle_keeps_draft_and_permission_colors(size: tuple[int, int]):
    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Toggle must not start work"))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        toggle = app.query_one("#prime-toggle", Button)
        row = app.query_one("#prompt-row")
        prompt.text = "Keep this draft @file.py"
        original_border = row.styles.border_top
        await pilot.click("#prime-toggle")
        await pilot.pause()
        assert app.session_state.prime_enabled
        assert str(toggle.label) == "Prime: on"
        assert app.screen.has_class("prime-mode")
        assert row.has_class("prime-composer")
        assert row.border_title == "Nova Prime · Deep Work"
        assert row.styles.border_top != original_border
        assert prompt.text == "Keep this draft @file.py"
        assert app.focused is prompt
        for selector in (
            "#prime-toggle",
            "#plan-toggle",
            "#caveman-toggle",
            "#passing-toggle",
            "#code-mode-toggle",
        ):
            control = app.query_one(selector, Button)
            assert control.region.right <= size[0]
            assert control.region.bottom <= size[1]
            assert control.region.height == 1
        # Safety and command modes retain their established colors and labels.
        app.session_state.plan_mode_enabled = True
        app._update_mode_badge()
        assert row.has_class("plan-mode")
        assert not row.has_class("prime-composer")
        app.session_state.plan_mode_enabled = False
        app.session_state.auto_approve = True
        app._update_mode_badge()
        assert row.has_class("auto-approve-mode")
        assert not row.has_class("prime-composer")
        app._update_mode_badge("!pwd")
        assert row.has_class("bash-mode")
        assert not row.has_class("prime-composer")
        app.session_state.auto_approve = False
        app._update_mode_badge(prompt.text)
        # The same control supports keyboard activation.
        toggle.focus()
        await pilot.pause()
        assert app.focused is toggle
        await pilot.press("enter")
        await pilot.pause()
        assert not app.session_state.prime_enabled
        assert not app.screen.has_class("prime-mode")
        assert not row.has_class("prime-composer")
        assert not row.border_title
        assert row.styles.border_top == original_border
        assert prompt.text == "Keep this draft @file.py"
        app.agent.astream.assert_not_called()


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(110, 38), (55, 24)])
async def test_plan_composer_toggle_uses_plan_agent_and_keeps_draft(  # noqa: PLR0915 -- One UI lifecycle.
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.agents import plan_agent

    app = _app()
    app.agent.astream = AsyncMock(side_effect=AssertionError("Toggle must not start work"))
    app.session_state._model = Mock()
    planner, backend = Mock(), Mock()
    factory = Mock(return_value=(planner, backend))
    monkeypatch.setattr(plan_agent, "create_plan_agent_with_config", factory)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        toggle = app.query_one("#plan-toggle", Button)
        row = app.query_one("#prompt-row")
        prompt.text = "Investigate @parser.py"
        original_border = row.styles.border_top
        original_approval = app.session_state.auto_approve
        log = Mock(wraps=app._log)
        monkeypatch.setattr(app, "_log", log)
        assert await pilot.click(toggle)
        await pilot.pause()
        factory.assert_called_once()
        assert app.session_state.plan_mode_enabled
        assert app._active_agent() == (planner, backend)
        assert str(toggle.label) == "Plan: on"
        assert toggle.has_class("enabled")
        assert app.screen.has_class("planning-mode")
        assert row.has_class("plan-composer")
        assert row.border_title == "Plan Mode · Read only"
        assert row.styles.border_top != original_border
        assert app.focused is prompt
        assert prompt.text == "Investigate @parser.py"
        assert app.session_state.auto_approve is original_approval
        log.assert_not_called()
        # Both modes can be enabled; Plan keeps the read-only frame.
        await app._run_prime("/prime on")
        assert row.has_class("plan-composer")
        assert not row.has_class("prime-composer")
        log.assert_not_called()
        await app._run_plan("/plan status")
        assert "enabled" in str(log.call_args.args[0])
        log.reset_mock()
        for selector in (
            "#prime-toggle",
            "#plan-toggle",
            "#caveman-toggle",
            "#passing-toggle",
            "#code-mode-toggle",
        ):
            control = app.query_one(selector, Button)
            assert 0 <= control.region.x < control.region.right <= size[0]
            assert control.region.bottom <= size[1]
            assert control.region.height == 1
        toggle.focus()
        await pilot.pause()
        assert app.focused is toggle
        await pilot.press("enter")
        await pilot.pause()
        assert not app.session_state.plan_mode_enabled
        assert app._active_agent() == (app.agent, app.backend)
        assert not app.screen.has_class("planning-mode")
        assert str(toggle.label) == "Plan: off"
        assert row.has_class("prime-composer")
        assert prompt.text == "Investigate @parser.py"
        assert app.focused is prompt
        log.assert_not_called()
        app.agent.astream.assert_not_called()

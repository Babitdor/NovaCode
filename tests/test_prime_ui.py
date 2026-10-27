"""Prime controls use the existing prompt modes and do not start agent work."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Static

from novacode_cli import ui_events as ev
from novacode_cli.prime import PrimeRun
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
            "#caveman-toggle",
            "#caveman-options",
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
        await pilot.press("enter")
        await pilot.pause()
        assert not app.session_state.prime_enabled
        assert not app.screen.has_class("prime-mode")
        assert not row.has_class("prime-composer")
        assert not row.border_title
        assert row.styles.border_top == original_border
        assert prompt.text == "Keep this draft @file.py"
        app.agent.astream.assert_not_called()

"""Prime controls use the existing prompt modes and do not start agent work."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from textual.widgets import Static

from novacode_cli import ui_events as ev
from novacode_cli.prime import PrimeRun
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
        assert app.session_state.auto_approve
        assert "PRIME" not in str(badge.render())
        app.agent.astream.assert_not_called()

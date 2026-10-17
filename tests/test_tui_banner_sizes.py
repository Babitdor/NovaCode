"""The classic home banner shows NOVA at any window size it can fit in."""

from __future__ import annotations

import pytest

from novacode_cli.config.nova_config import NovaConfig
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401

SIZES = [(170, 50), (100, 30), (100, 29), (80, 24), (60, 21), (80, 17), (80, 16)]


@pytest.mark.parametrize(("width", "height"), SIZES)
async def test_classic_banner_shows_and_fits(width: int, height: int) -> None:
    NovaConfig().set("home_dashboard_enabled", False)
    app = _app()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        header = app.query_one("#home-header")
        rain = app._home_banner
        prompt = app.query_one("#prompt-dock")
        shown = header.display and header.region.height > 0
        assert shown is (height >= 17), "shown whenever it fits, hidden below that"
        if shown:
            art = "".join(
                segment.text for y in range(rain.region.height) for segment in rain.render_line(y)
            )
            assert "███╗" in art, "the NOVA wordmark is on screen"
            assert rain.region.bottom <= prompt.region.y, "the banner overlaps the prompt"
            assert header.region.width == width, "no scrollbar: the home screen fits"

"""Skills controls stay usable in narrow and short terminals."""

import pytest
from textual.app import App
from textual.widgets import Button, OptionList

from novacode_cli.tui.screens import SkillsScreen


class SkillsApp(App):
    _skill_names_cache = ["review"]
    assistant_id = "test"
    _skill_count_cache = None
    _status_tail = None

    def _get_skill_names(self):
        return ["review"]

    def _collect_skill_names(self):
        return ["review", "testing"]

    def _refresh_status(self):
        pass


@pytest.mark.parametrize("size", [(100, 35), (60, 20), (40, 16)])
@pytest.mark.asyncio
async def test_pin_reload_controls_fit(size, monkeypatch):
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.load_disabled", lambda *a: set())
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    monkeypatch.setattr(SkillsScreen, "_update_preview", lambda *a: None)
    app = SkillsApp()
    async with app.run_test(size=size) as pilot:
        screen = SkillsScreen()
        if size[0] < 80:
            screen.add_class("narrow")
        app.push_screen(screen)
        await pilot.pause()
        for button in screen.query(Button):
            assert button.region.width > 0
            assert button.region.right <= size[0]
            assert button.region.bottom <= size[1]
        screen.query_one("#reload", Button).press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app._skill_names_cache == ["review", "testing"]
        assert screen.query_one("#skills-list", OptionList).option_count == 1
        selected = []
        app.pop_screen()
        screen = SkillsScreen()
        app.push_screen(screen, selected.append)
        await pilot.pause()
        screen.query_one("#pin", Button).press()
        await pilot.pause()
        assert selected == ["review"]

"""Character scenes and motion retain input room and obey reduced-motion controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.text import Text
from textual.widgets import Switch

from novacode_cli import brand
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.home_scene import HOME_SCENE_FPS, HomeScene, scene_frame
from novacode_cli.tui.screens import ThemeScreen
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, _resize_terminal, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.timeout(30)
async def test_resize_waits_for_screen_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.geometry import Size

    app = _app()
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        original_timer = app.set_timer

        def delayed_timer(delay: float, callback: object, **kwargs: object) -> object:
            return original_timer(0.25 if delay == 1 / 120 else delay, callback, **kwargs)

        monkeypatch.setattr(app, "set_timer", delayed_timer)
        await _resize_terminal(pilot, 48, 18)
        assert app.screen.size == Size(48, 18)
        assert app.query_one("#prompt-dock").region.right <= 48


def test_character_is_the_existing_brand_portrait():
    assert brand.compact_art()[: len(brand.portrait())] == brand.portrait()
    assert len(brand.portrait()) == 15


@pytest.mark.parametrize("style", ["terminal", "blueprint", "studio"])
def test_frames_change_without_layout_jumps(style: str):
    app = _app()
    base = (
        Text("$ nova\nYour next change starts here.")
        if style == "terminal"
        else Text(
            "NOVA\n────────────────────────────"
            if style == "blueprint"
            else "\n".join(brand.lockup())
        )
    )
    first = scene_frame(style, base, app._palette, 0)
    next_frame = scene_frame(style, base, app._palette, 5)
    assert first != next_frame
    assert first.plain.count("\n") == next_frame.plain.count("\n") == base.plain.count("\n")
    assert base.spans == []  # Rendering must not mutate the shared brand art.


@pytest.mark.parametrize("style", ["workbench", "studio"])
@pytest.mark.timeout(30)
async def test_portrait_home_has_room_for_actions_and_composer(style: str, tmp_path: Path):
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        scene = app.query_one("#home-style-art", HomeScene)
        assert app.screen.has_class("scene-roomy")
        assert scene.region.height == 15
        assert brand.portrait()[0].strip() in scene._base.plain
        dock = app.query_one("#prompt-dock")
        assert scene.region.bottom <= dock.region.y
        actions = app.query_one("#home-style-actions")
        assert actions.region.bottom <= dock.region.y
        (tmp_path / f"{style}-portrait.svg").write_text(app.export_screenshot(), encoding="utf-8")
        await _resize_terminal(pilot, 80, 25)
        await pilot.pause()
        assert not app.screen.has_class("scene-roomy")
        assert not scene.display
        assert dock.region.bottom <= app.size.height


@pytest.mark.timeout(30)
async def test_animation_pauses_for_modal_blur_low_resource_and_saved_opt_out():
    NovaConfig().set("home_style", "studio")
    app = _app()
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        scene = app.query_one(HomeScene)
        assert app._home_scene_can_animate()
        app._schedule_status_tick()
        assert app._status_timer._interval == pytest.approx(1 / HOME_SCENE_FPS)
        scene.advance(scene._started + 0.1, animated=True)
        before = scene._shown
        scene.advance(scene._started + 0.9, animated=True)
        assert scene._shown != before
        app._os_focused = False
        app._paint_home_scene()
        assert scene._shown == (False, -1)
        app._os_focused = True
        app._low_resource_mode = True
        app._set_animation_fps(15)
        assert scene._shown == (False, -1)
        app._low_resource_mode = False
        screen = ThemeScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert not app._home_scene_can_animate()
        screen.query_one("#home-animations-toggle", Switch).value = False
        await pilot.pause()
        assert NovaConfig().get("home_animations_enabled") is False
        screen.dismiss()
        await pilot.pause()
        assert not app._home_scene_can_animate()
        assert scene._shown == (False, -1)
        app._set_home_visible(visible=False)
        assert not app._home_scene_can_animate()

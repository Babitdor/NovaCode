"""Resume dialogs preserve identities, context choices and drafts across designs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button, Input, OptionList, Static

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.session.session_persistence import SessionMeta
from novacode_cli.tui.pickers import SessionPickerApp
from novacode_cli.tui.resume_picker import ResumeModeScreen, ResumeSessionScreen
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def sessions() -> list[SessionMeta]:
    return [
        SessionMeta(
            session_id=f"8a31e7d{index}-abcdefgh",
            thread_id=f"thread-{index}",
            created_at="2026-10-19T09:00:00+00:00",
            last_active="2026-10-19T10:30:00+00:00",
            project_root=f"B:/Projects/{project}",
            repo_hash=None,
            Nova_md_checksum=None,
            model_name="saved-model",
            model_provider="ollama" if index else None,
            assistant_id="nova",
            message_count=12 + index,
            current_task=task,
            task_status="blocked" if index == 2 else "active",
            next_step_hint="Run the router tests" if index == 2 else None,
        )
        for index, (project, task) in enumerate(
            (
                ("alpha", "Improve the prompt composer"),
                ("beta", "Repair the request router"),
                ("gamma", "Verify session restore"),
            )
        )
    ]


@pytest.mark.parametrize("compact", [True, False])
async def test_search_returns_original_session_and_preserves_resume_modes(
    sessions: list[SessionMeta],
    tmp_path: Path,
    *,
    compact: bool,
) -> None:
    sessions[1].current_task = "Repair the router [bold]literal[/bold]"
    app = SessionPickerApp(sessions)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ResumeSessionScreen)
        await pilot.press("ctrl+f")
        assert screen.query_one("#resume-search", Input).has_focus
        screen.query_one(Input).value = "beta router"
        await pilot.pause()
        assert screen.query_one(OptionList).option_count == 1
        assert sessions[1].current_task in str(screen.query_one("#resume-task", Static).render())
        if not compact:
            screen.query_one(OptionList).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ResumeModeScreen)
        (tmp_path / "resume-mode.svg").write_text(app.export_screenshot(), encoding="utf-8")
        if not compact:
            await pilot.press("down")
        await pilot.press("enter")
    assert app.return_value == (sessions[1].session_id, compact)


async def test_empty_search_and_missing_sessions_never_resume(sessions: list[SessionMeta]) -> None:
    for metadata in (sessions, []):
        app = SessionPickerApp(metadata)
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            search = screen.query_one(Input)
            search.value = "no matching session"
            await pilot.pause()
            assert screen.query_one("#select", Button).disabled
            assert screen.query_one("#resume-empty").display
            search.focus()
            await pilot.press("enter")
            assert app.screen is screen
            if metadata:
                search.value = ""
                await pilot.pause()
                assert not screen.query_one("#select", Button).disabled
                assert screen.query_one(OptionList).option_count == len(metadata)
            await pilot.press("escape")
        assert app.return_value is None


@pytest.mark.parametrize("style", ["dashboard", "classic", "centered", "banner", "minimal"])
async def test_dialog_matches_design_and_keeps_draft_at_small_sizes(
    sessions: list[SessionMeta],
    style: str,
    tmp_path: Path,
) -> None:
    NovaConfig().set("home_style", style)
    app = _app()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        prompt.value = "Keep my draft\nand its second line"
        selected: list[int] = []
        screen = ResumeSessionScreen(sessions)
        app.push_screen(screen, selected.append)
        await pilot.pause()
        assert screen.has_class(f"{style}-ui")
        assert screen.query_one("#resume-details").size.width > 0
        assert "Model not restored" in str(screen.query_one("#resume-model", Static).render())
        listing = screen.query_one(OptionList)
        listing.focus()
        await pilot.press("down", "down")
        await pilot.pause()
        assert "Run the router tests" in str(screen.query_one("#resume-next", Static).render())
        assert screen.query_one("#resume-status").has_class("resume-blocked")
        (tmp_path / f"resume-{style}.svg").write_text(app.export_screenshot(), encoding="utf-8")
        for width, height in ((80, 26), (48, 16), (140, 44)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            for key in ("select", "cancel"):
                button = screen.query_one(f"#{key}")
                assert button.region.bottom <= height
                assert 0 <= button.region.x < button.region.right <= width
            assert screen.query_one(OptionList).size.height > 0
            if width < 96:
                assert not screen.query_one("#resume-details").display
            if height == 16:
                painted = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                assert "14 messages" in painted
                (tmp_path / f"resume-{style}-compact.svg").write_text(
                    app.export_screenshot(), encoding="utf-8"
                )
        await pilot.click("#select")
        await pilot.pause()
        assert selected == [2]
        assert prompt.value == "Keep my draft\nand its second line"


async def test_standalone_picker_loads_saved_design_and_palette(
    sessions: list[SessionMeta],
) -> None:
    NovaConfig().set("home_style", "classic")
    NovaConfig().set("theme", "nova-paper")
    app = SessionPickerApp(sessions)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.theme == "nova-paper"
        assert app.screen.has_class("classic-ui")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.has_class("classic-ui")
        await pilot.resize_terminal(48, 16)
        await pilot.pause()
        assert app.screen.query_one("#select").region.bottom <= app.size.height
        assert app.screen.query_one(OptionList).size.height > 0
        await pilot.press("escape")
    assert app.return_value is None

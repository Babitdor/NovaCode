"""The compact composer footer: session facts line + modes line + shift+tab.

Shape (Claude Code / Gemini CLI style), rendered only in the conversation
layouts where the labelled info-bar columns are off duty:

    GLM-5.3 Flash with high effort · ctx 401k/1.0M · 42% · 58% left · <cwd>
    · branch: main · <session> · v1.2.56
    ⏵ accept edits off (shift+tab) · ? for shortcuts
"""

from __future__ import annotations

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")


@pytest.fixture(autouse=True)
def _offline_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    import novacode_cli.config.config as config_module

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")
    from novacode_cli.config.nova_config import NovaConfig

    def isolated_config(config):
        config.config_dir = tmp_path / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {}
        config._loaded = {}
        config._load()

    monkeypatch.setattr(NovaConfig, "__init__", isolated_config)


class _FakeAgent:
    async def aget_state(self, config):
        class _State:
            values = {"messages": []}

        return _State()


class _SS:
    thread_id = "t1"
    session_id = "s1"
    auto_approve = False
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


def _app(home_style: str = "minimal"):
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    app._home_style = home_style
    return app


async def test_conversation_layouts_show_the_two_line_strip() -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        await pilot.pause()
        info = app.query_one("#composer-info")
        modes = app.query_one("#composer-modes")
        assert info.display is True
        assert modes.display is True
        text = str(info.render())
        assert "test-model" in text
        assert "ctx" in text
        assert "v1.2." in text
        assert "accept edits off (shift+tab)" in str(modes.render())
        assert "? for shortcuts" in str(modes.render())


async def test_home_layouts_keep_the_info_bar_instead() -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "classic"
        app._refresh_info_bar()
        await pilot.pause()
        assert app.query_one("#composer-info").display is False
        assert app.query_one("#composer-modes").display is False
        assert app.query_one("#info-bar").display is True


async def test_shift_tab_cycles_accept_edits_then_plan_mode() -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        await pilot.pause()
        state = app.session_state
        assert state.auto_approve is False and state.plan_mode_enabled is False

        await pilot.press("shift+tab")
        await pilot.pause()
        assert state.auto_approve is True
        assert "accept edits on" in str(app.query_one("#composer-modes").render())

        await pilot.press("shift+tab")
        await pilot.pause()
        assert state.auto_approve is False
        assert state.plan_mode_enabled is True
        assert "plan mode" in str(app.query_one("#composer-modes").render())

        await pilot.press("shift+tab")
        await pilot.pause()
        assert state.auto_approve is False and state.plan_mode_enabled is False
        assert "accept edits off" in str(app.query_one("#composer-modes").render())


async def test_effort_suffix_shows_only_when_the_model_consumes_it(monkeypatch) -> None:
    """A model that silently drops effort must not be advertised with it."""
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        app._reasoning_effort = "high"
        monkeypatch.setattr(app, "_model_provider", "opencode", raising=False)
        await pilot.pause()

        # A model family with no verified effort control: plain name only.
        app.model_name = "some-unknown-model"
        app._effort_accepts_cache = None
        app._refresh_composer_info()
        await pilot.pause()
        head = str(app.query_one("#composer-info").render()).split(" · ")[0]
        assert head == "some-unknown-model", "no effort claim for a model that drops it"

        # A verified family shows the suffix.
        app.model_name = "glm-5.3-flash"
        app._effort_accepts_cache = None
        app._refresh_composer_info()
        await pilot.pause()
        head = str(app.query_one("#composer-info").render()).split(" · ")[0]
        assert head == "glm-5.3-flash with high"


async def test_plan_mode_indicator_is_plain_text() -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        await pilot.pause()
        app.session_state.plan_mode_enabled = True
        app._refresh_composer_modes()
        await pilot.pause()
        modes = str(app.query_one("#composer-modes").render())
        assert "plan mode" in modes
        assert "⏸" not in modes, "the plan-mode indicator is plain text"


async def test_question_mark_on_empty_composer_opens_the_help_reference() -> None:
    from novacode_cli.tui.reference_screens import HelpScreen

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause(0.2)
        assert any(isinstance(screen, HelpScreen) for screen in app.screen_stack), (
            "? must open the shortcuts/command reference"
        )


async def test_question_mark_mid_text_still_types_itself() -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.text = "what"
        prompt.move_cursor(prompt.document.end)
        await pilot.press("?")
        await pilot.pause()
        assert prompt.text == "what?"

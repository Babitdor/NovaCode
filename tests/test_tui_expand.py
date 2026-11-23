"""ctrl+o expands/collapses every tool surface in the TUI.

Like the other coding-agent CLIs: collapsed tool content advertises
``ctrl+o to expand``, and the chord flips all tool surfaces at once for the
session (CodeModePanel cards, history tool cards, the condensed tool group).
"""

from __future__ import annotations

import pytest

from novacode_cli.ui import status_phrases  # noqa: F401 — mirrors test_tui_app import order

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")


@pytest.fixture(autouse=True)
def _offline_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """TUI tests never contact update services or write the real user cache."""
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
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )


async def test_display_tail_expanded_limits() -> None:
    from novacode_cli.tui.app import _display_tail

    assert _display_tail("short\n") == "short\n"
    big = "\n".join(f"line {i}" for i in range(500)) + "\n"
    assert "output trimmed" in _display_tail(big)
    # Expanded mode raises the ceiling: 500 lines pass through untouched.
    assert _display_tail(big, expanded=True) == big


async def test_tool_group_title_carries_expand_hint() -> None:
    from textual.widgets import Collapsible

    app = _app()
    async with app.run_test() as pilot:
        await app._ensure_tool_group()
        group = app._tool_group
        assert isinstance(group, Collapsible)
        assert group.collapsed
        app._refresh_tool_group(running=None)
        await pilot.pause()
        assert "ctrl+o to expand" in str(group.title)
        # Once expanded (or the group opened), the hint goes away.
        group.collapsed = False
        app._refresh_tool_group(running=None)
        await pilot.pause()
        assert "ctrl+o to expand" not in str(group.title)


async def test_ctrl_o_expands_and_collapses_all_tool_surfaces() -> None:
    from textual.widgets import Collapsible

    from novacode_cli.tui.code_mode_panel import CodeModePanel

    app = _app()
    async with app.run_test() as pilot:
        await app._ensure_tool_group()
        assert app._tool_group is not None
        panel = CodeModePanel.for_call("shell", {"command": "ls"}, live=False)
        panel.finish("done", is_error=False, preview="done")
        await app._transcript().mount(panel)
        await pilot.pause()

        assert app._tool_group.collapsed
        assert panel.collapsed

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert app._tools_expanded
        assert app._tool_group.collapsed is False
        assert panel.collapsed is False
        assert "ctrl+o to expand" not in app._tool_group.title.plain

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert not app._tools_expanded
        assert panel.collapsed is True
        assert app._tool_group.collapsed is True
        assert "ctrl+o to expand" in app._tool_group.title.plain


async def test_ctrl_o_leaves_streaming_group_open_on_collapse() -> None:
    app = _app()
    async with app.run_test() as pilot:
        await app._ensure_tool_group()
        assert app._tool_group is not None
        app._tool_group.collapsed = False
        app._tool_group_running = "shell"

        await pilot.press("ctrl+o")  # expand
        await pilot.press("ctrl+o")  # collapse — a tool is still running
        await pilot.pause()
        assert not app._tools_expanded
        assert app._tool_group.collapsed is False

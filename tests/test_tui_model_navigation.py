"""Model rebuilds must not print over the TUI; empty-composer navigation stays local."""

import asyncio
import io
from unittest.mock import Mock

import pytest
from rich.console import Console
from textual.app import App, ComposeResult

from novacode_cli.tui.widgets import PromptInput


class NavigationApp(App):
    opened = 0

    def compose(self) -> ComposeResult:
        yield PromptInput(id="prompt")

    def on_prompt_input_command_center_requested(self) -> None:
        self.opened += 1


async def test_left_opens_overview_only_for_empty_composer():
    app = NavigationApp()
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("left")
        assert app.opened == 1
        prompt.text = "hello"
        prompt.move_cursor((0, 5))
        await pilot.press("left")
        assert prompt.cursor_location == (0, 4)
        assert app.opened == 1


async def test_rebuild_captures_output_in_worker_thread(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.agents import core_agent
    from novacode_cli.config import config
    from novacode_cli.states.slices.agent_runtime import _build_agent_for_switch

    output = io.StringIO()
    console = Console(file=output)
    monkeypatch.setattr(config, "console", console)

    def build(**_kwargs: object) -> tuple[str, str]:
        console.print("mcp: linking servers")
        return "agent", "backend"

    monkeypatch.setattr(core_agent, "create_agent_with_config", build)
    async with NavigationApp().run_test():
        assert await asyncio.to_thread(_build_agent_for_switch) == ("agent", "backend")
    assert output.getvalue() == ""
    assert _build_agent_for_switch() == ("agent", "backend")
    assert "linking servers" in output.getvalue()


def test_command_center_request_starts_worker():
    from novacode_cli.tui.app import NovaApp

    app = Mock()
    app._run_command_center.return_value = "command-center-coroutine"
    event = Mock()
    NovaApp.on_prompt_input_command_center_requested(app, event)
    event.stop.assert_called_once()
    app.run_worker.assert_called_once_with(
        "command-center-coroutine",
        group="command-center",
        exclusive=True,
    )


@pytest.mark.parametrize("operation", ["switch_model", "reload_mcp_servers"])
async def test_runtime_rebuild_paths_capture_in_tui(
    operation: str, monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli import mcp
    from novacode_cli.agents import core_agent
    from novacode_cli.config import config
    from novacode_cli.states.slices.agent_runtime import AgentRuntimeState

    output = io.StringIO()
    console = Console(file=output)
    monkeypatch.setattr(config, "console", console)
    monkeypatch.setattr(mcp, "reset_shared_mcp_middleware", lambda: None)

    def build(**_kwargs: object) -> tuple[str, str]:
        console.print("MCP and plugin discovery")
        return "new-agent", "new-backend"

    monkeypatch.setattr(core_agent, "create_agent_with_config", build)
    state = AgentRuntimeState()
    state._agent = object()
    state._store = object()
    state._checkpointer = object()
    state._assistant_id = "test"
    async with NavigationApp().run_test():
        if operation == "switch_model":
            result = await state.switch_model(object(), notify_model_switch=False)
        else:
            result = await state.reload_mcp_servers()
    assert result == ("new-agent", "new-backend")
    assert output.getvalue() == ""


async def test_quiet_setup_preserves_result_and_exceptions(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.tui import app

    output = io.StringIO()
    console = Console(file=output)
    monkeypatch.setattr(app, "_rich_console", console)

    def setup(*, fail: bool = False) -> str:
        console.print("Using model")
        if fail:
            message = "setup failed"
            raise ValueError(message)
        return "model"

    assert await asyncio.to_thread(app._quiet_call, setup) == "model"
    with pytest.raises(ValueError, match="setup failed"):
        await asyncio.to_thread(app._quiet_call, setup, fail=True)
    assert output.getvalue() == ""
    console.print("capture restored")
    assert "capture restored" in output.getvalue()

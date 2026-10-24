"""Composer preference controls existing MCP Code Mode graphs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from textual.widgets import Button

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.mcp.code_mode import MCPCodeModeMiddleware
from tests.test_mcp_code_mode import (
    ScriptedModel,
    execute,
    inventory,
    isolated_policy,  # noqa: F401
)
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    from pathlib import Path


# Two full app boots plus pauses: past the 10 s default under coverage on Windows CI.
@pytest.mark.timeout(60)
@pytest.mark.parametrize("size", [(80, 30), (45, 25)])
async def test_composer_toggle_persists_and_supports_keyboard(
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        button = app.query_one("#code-mode-toggle", Button)
        assert str(button.label) == "Code Mode: on"
        assert button.has_class("enabled")
        assert button.region.height == 1
        assert button.region.right <= size[0]
        assert button.region.y >= app.query_one("#prompt").region.bottom
        assert await pilot.click(button)
        await pilot.pause()
        assert not NovaConfig().get_code_mode_enabled()
        assert str(button.label) == "Code Mode: off"
        assert not button.has_class("enabled")
        assert app.query_one("#prompt").has_focus
        await pilot.pause(0.4)
        button.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert NovaConfig().get_code_mode_enabled()
        await pilot.pause(0.4)
        await pilot.click(button)
        await pilot.pause()
        assert not NovaConfig().get_code_mode_enabled()
    app = _app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert str(app.query_one("#code-mode-toggle", Button).label) == "Code Mode: off"


@pytest.mark.timeout(60)
async def test_failed_save_preserves_toggle(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            NovaConfig, "set_code_mode_enabled", Mock(side_effect=OSError("read only"))
        )
        await pilot.click("#code-mode-toggle")
        await pilot.pause()
        assert NovaConfig().get_code_mode_enabled()
        assert str(app.query_one("#code-mode-toggle", Button).label) == "Code Mode: on"


async def test_live_graph_hides_and_rejects_code_but_keeps_other_tools(tmp_path: Path):
    calls = []

    @tool("db_read")
    def read() -> str:
        """Read a synthetic value."""
        calls.append(True)
        return "ok"

    mcp = inventory(tmp_path, read)
    middleware = MCPCodeModeMiddleware(mcp)
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=[],
        state={},
        tools=[*middleware.tools, read, {"name": "other", "description": "Other tool"}],
    )
    sync = Mock()
    middleware.wrap_model_call(request, sync)
    assert sync.call_args.args[0] is request
    model = ScriptedModel(
        messages=iter(
            [
                message
                for _ in range(2)
                for message in [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "outer",
                                "name": "MCPCode",
                                "args": {"code": 'return await mcp.call("db", "read", {});'},
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ]
        )
    )
    g = create_agent(model, middleware=[middleware, mcp])
    NovaConfig().set_code_mode_enabled(False)
    middleware.wrap_model_call(request, sync)
    assert sync.call_args.args[0].tools == request.tools[1:]
    handler = AsyncMock()
    await middleware.awrap_model_call(request, handler)
    assert handler.call_args.args[0].tools == request.tools[1:]
    assert len(request.tools) == 3
    assert "MCPCode" not in mcp._inject_mcp_section(request).system_message.text
    messages = await execute(g)
    assert messages[0].status == "error"
    assert "disabled" in messages[0].content
    assert calls == []
    NovaConfig().set_code_mode_enabled(True)
    await middleware.awrap_model_call(request, handler)
    assert handler.call_args.args[0] is request
    assert "MCPCode" in mcp._inject_mcp_section(request).system_message.text
    assert (await execute(g))[0].status == "success"
    assert calls == [True]

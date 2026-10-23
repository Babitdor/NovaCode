"""Response style changes live without rewriting user input or stored messages."""

from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from textual.widgets import Button

from novacode_cli.agents.caveman import CAVEMAN_INSTRUCTIONS, CavemanMiddleware
from novacode_cli.config.nova_config import NovaConfig
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app


def _request(system: SystemMessage | None = None) -> ModelRequest:
    return ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=[
            HumanMessage(content="Fix `x != 2`; explain in detail"),
            ToolMessage(content="Error: file not found", tool_call_id="test"),
        ],
        system_message=system,
    )


@pytest.mark.parametrize(("saved", "enabled"), [(None, False), ("false", False), (True, True)])
def test_preference_is_opt_in_and_persists(saved: object, *, enabled: bool):
    if saved is not None:
        NovaConfig().set("caveman_enabled", saved)
    assert NovaConfig().get_caveman_enabled() is enabled
    NovaConfig().set_caveman_enabled(not enabled)
    assert NovaConfig().get_caveman_enabled() is not enabled


@pytest.mark.parametrize("system", [
    None,
    SystemMessage(content="Core coding rules"),
    SystemMessage(content=[{
        "type": "text", "text": "Core coding rules", "cache_control": {"type": "ephemeral"},
    }]),
])
def test_style_preserves_original_history_and_system_metadata(system: SystemMessage | None):
    req = _request(system)
    assert CavemanMiddleware.prepare(req, enabled=False) is req
    prepared = CavemanMiddleware.prepare(req, enabled=True)
    assert prepared.messages == req.messages
    assert prepared.model is req.model
    assert CAVEMAN_INSTRUCTIONS in prepared.system_message.text
    if system is not None:
        assert CAVEMAN_INSTRUCTIONS not in system.text
        if isinstance(system.content, list):
            assert prepared.system_message.content[0] == system.content[0]


async def test_existing_middleware_observes_toggle_on_next_sync_or_async_call():
    middleware = CavemanMiddleware()
    req = _request(SystemMessage(content="Core rules"))
    sync = Mock(return_value="sync result")
    assert middleware.wrap_model_call(req, sync) == "sync result"
    assert sync.call_args.args[0] is req
    NovaConfig().set_caveman_enabled(True)
    handler = AsyncMock(return_value="async result")
    assert await middleware.awrap_model_call(req, handler) == "async result"
    assert CAVEMAN_INSTRUCTIONS in handler.call_args.args[0].system_message.text
    NovaConfig().set_caveman_enabled(False)
    await middleware.awrap_model_call(req, handler)
    assert handler.call_args.args[0] is req


@pytest.mark.parametrize("size", [(80, 30), (45, 25)])
async def test_composer_toggle_click_keyboard_commands_and_restart(
    size: tuple[int, int], monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        button = app.query_one("#caveman-toggle", Button)
        assert button.region.width > 0
        assert button.region.height == 1
        assert button.region.right <= size[0]
        assert str(button.label) == "Caveman: off"
        assert await pilot.click("#caveman-toggle")
        await pilot.pause()
        assert NovaConfig().get_caveman_enabled()
        assert str(button.label) == "Caveman: on"
        await pilot.pause(0.4)  # Button ignores repeat presses during its active animation.
        button.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not NovaConfig().get_caveman_enabled()
        await app._run_caveman("/caveman on")
        await app._run_caveman("/caveman status")
        await app._run_caveman("/caveman invalid")
        assert NovaConfig().get_caveman_enabled()
    app = _app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert str(app.query_one("#caveman-toggle", Button).label) == "Caveman: on"


async def test_failed_save_keeps_current_toggle(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            NovaConfig, "set_caveman_enabled", Mock(side_effect=OSError("read only"))
        )
        await app._set_caveman_enabled(enabled=True)
        assert not app._caveman_enabled
        assert str(app.query_one("#caveman-toggle", Button).label) == "Caveman: off"


async def test_toggle_stays_visible_across_home_styles(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.tui.home import HOME_STYLES

    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(80, 32)) as pilot:
        for _, style in HOME_STYLES:
            app._set_home_style(style)
            await pilot.pause()
            button = app.query_one("#caveman-toggle", Button)
            assert button.region.height == 1, style
            assert button.region.bottom <= 32, style
            assert button.region.y >= app.query_one("#prompt").region.bottom, style
        app._set_home_visible(visible=False)
        await pilot.pause()
        assert app.query_one("#caveman-toggle", Button).region.height == 1


async def test_compiled_graph_reads_live_preference_and_keeps_security_boundary():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from novacode_cli.security.prompt_injection import PromptInjectionMiddleware

    seen = []

    class Model(GenericFakeChatModel):
        def _generate(self, messages, *args, **kwargs) -> ChatResult:  # noqa: ANN001, ANN002, ANN003
            seen.append(messages)
            return super()._generate(messages, *args, **kwargs)

    graph = create_agent(
        Model(messages=iter([AIMessage(content="ok")] * 3)),
        system_prompt="Core instructions",
        middleware=[CavemanMiddleware(), PromptInjectionMiddleware()],
    )
    inputs = {"messages": [HumanMessage(content="Fix the bug")]}
    await graph.ainvoke(inputs)
    NovaConfig().set_caveman_enabled(True)
    state = await graph.ainvoke(inputs)
    NovaConfig().set_caveman_enabled(False)
    await graph.ainvoke(inputs)
    assert CAVEMAN_INSTRUCTIONS not in seen[0][0].text
    assert CAVEMAN_INSTRUCTIONS in seen[1][0].text
    assert CAVEMAN_INSTRUCTIONS not in seen[2][0].text
    assert all("Nova security trust boundaries" in call[0].text for call in seen)
    assert not any(isinstance(message, SystemMessage) for message in state["messages"])

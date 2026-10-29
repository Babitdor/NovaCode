"""Response style changes live without rewriting user input or stored messages."""

from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from textual.widgets import Button, Select

from novacode_cli.agents.caveman import (
    CAVEMAN_INSTRUCTIONS,
    CAVEMAN_STYLES,
    CavemanMiddleware,
    caveman_instructions,
)
from novacode_cli.config.nova_config import CAVEMAN_LEVELS, NovaConfig
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


@pytest.mark.parametrize(
    "system",
    [
        None,
        SystemMessage(content="Core coding rules"),
        SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": "Core coding rules",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        ),
    ],
)
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


@pytest.mark.parametrize("level", CAVEMAN_LEVELS)
def test_skill_intensities_persist_and_apply_without_loading_other_levels(level: str):
    config = NovaConfig()
    assert config.get_caveman_level() == "full"
    config.set_caveman_level(level)
    assert NovaConfig().get_caveman_level() == level
    assert not config.get_caveman_enabled()
    config.set_caveman_enabled(True)
    middleware = CavemanMiddleware()
    prepared = middleware._prepare_live(_request())
    assert caveman_instructions(level) in prepared.system_message.text
    for other, style in CAVEMAN_STYLES.items():
        if other != level:
            assert "Intensity: " + style not in prepared.system_message.text
    # Every later response gets the same instruction; nothing is written to history.
    assert middleware._prepare_live(_request()).system_message == prepared.system_message
    assert prepared.messages == _request().messages


@pytest.mark.parametrize("invalid", [None, "invalid", [], 2])
def test_legacy_or_malformed_intensity_defaults_to_full(invalid: object):
    NovaConfig().set("caveman_level", invalid)
    assert NovaConfig().get_caveman_level() == "full"


def test_options_validation_does_not_partially_save_a_draft():
    config = NovaConfig()
    with pytest.raises(ValueError, match="intensity"):
        config.set_caveman_options(dict.fromkeys(config.get_caveman_features(), False), "bad")
    assert NovaConfig().get("caveman_features") is None
    assert NovaConfig().get_caveman_level() == "full"
    with pytest.raises(ValueError, match="intensity"):
        config.set_caveman_level("bad")


def test_skill_boundaries_and_auto_clarity_are_explicit_at_every_intensity():
    for level in CAVEMAN_LEVELS:
        note = caveman_instructions(level)
        for boundary in (
            "No tool-call narration",
            "dominant language",
            "negations",
            "Code stays exact; commit messages and PR descriptions use normal prose",
            "Never announce or name this style",
            "normal mode or to stop caveman",
            "security warnings",
            "irreversible confirmations",
            "order could be misread",
            "clarify/repeated question",
            "Resume terse prose",
            "skip verification",
        ):
            assert boundary in note
        assert note.index("These boundaries override intensity") > note.index("Intensity:")


@pytest.mark.parametrize("intensity", ["lite", "full", "ultra"])
@pytest.mark.parametrize("style_only", [False, True])
def test_wenyan_preserves_reply_language_and_uses_matching_non_chinese_intensity(
    intensity: str,
    *,
    style_only: bool,
):
    config = NovaConfig()
    config.set_caveman_enabled(True)
    config.set_caveman_level("wenyan-" + intensity)
    original = _request()
    prepared = CavemanMiddleware(style_only=style_only)._prepare_live(original)
    note = prepared.system_message.text
    assert "For Chinese replies only" in note
    assert "For replies in any other language" in note
    assert CAVEMAN_STYLES[intensity] in note
    assert "Language choice overrides intensity" in note
    assert "Selecting Wenyan does not request translation" in note
    assert "unless the user explicitly requests another language" in note
    assert note.index("Language choice overrides intensity") > note.index("Intensity:")
    assert prepared.messages == original.messages
    assert prepared.model is original.model


@pytest.mark.parametrize("structured", [True, False])
def test_selected_style_replaces_default_without_dropping_other_rules(*, structured: bool):
    from novacode_cli.prompts import TEMPLATES_DIR

    default = (TEMPLATES_DIR / "response_style.jinja").read_text(encoding="utf-8").strip()
    text = "<security>Keep checks</security>" + default + "End"
    content = (
        [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        if structured
        else text
    )
    system = SystemMessage(content=content)
    prepared = CavemanMiddleware.prepare(_request(system), enabled=True)
    result = prepared.system_message.text
    assert default not in result
    assert result.startswith("<security>Keep checks</security>")
    assert result.endswith("End")
    assert result.count(CAVEMAN_INSTRUCTIONS) == 1
    assert system.text == text
    if structured:
        assert prepared.system_message.content[0]["cache_control"] == {"type": "ephemeral"}


def test_custom_style_blocks_are_preserved_as_user_instructions():
    text = "<response_style>Include security constraints in answers.</response_style>"
    system = SystemMessage(content=text)
    prepared = CavemanMiddleware.prepare(_request(system), enabled=True)
    assert prepared.system_message.text.startswith(text)


def test_subagents_get_style_without_routing_compression_or_recovery_requirements():
    from novacode_cli.agents.core_agent import _harden_subagent_specs
    from novacode_cli.context.snapshot import background_context_middleware

    config = NovaConfig()
    config.set_caveman_enabled(True)
    config.set_caveman_level("ultra")
    specs = [{"name": "test", "system_prompt": "p", "middleware": []}]
    hardened = _harden_subagent_specs(_harden_subagent_specs(specs))
    for middlewares in (hardened[0]["middleware"], background_context_middleware()):
        instances = [m for m in middlewares if isinstance(m, CavemanMiddleware)]
        assert len(instances) == 1
        middleware = instances[0]
        assert middleware.before_agent({}, None) == {"caveman_route": None}
        assert middleware.before_model({}, None) is None
        original = _request()
        prepared = middleware._prepare_live(original)
        assert prepared.model is original.model
        assert prepared.messages == original.messages
        assert caveman_instructions("ultra") in prepared.system_message.text
        assert "Caveman utilities" not in prepared.system_message.text


@pytest.mark.timeout(30)
@pytest.mark.parametrize("size", [(80, 32), (45, 25)])
async def test_intensity_commands_and_options_save_cancel(
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=size) as pilot:
        for level in CAVEMAN_LEVELS:
            await app._run_lite_mode("/lite-mode " + level)
            assert NovaConfig().get_caveman_level() == level
            assert NovaConfig().get_caveman_enabled()
        await app._run_lite_mode("/lite-mode settings")
        await pilot.pause()
        picker = app.screen.query_one("#caveman-level", Select)
        assert picker.value == "wenyan-ultra"
        picker.value = "lite"
        await pilot.click("#caveman-save")
        await pilot.pause()
        assert NovaConfig().get_caveman_level() == "lite"
        await app._run_lite_mode("/lite-mode settings")
        await pilot.pause()
        app.screen.query_one("#caveman-level", Select).value = "full"
        await pilot.press("escape")
        await pilot.pause()
        assert NovaConfig().get_caveman_level() == "lite"
        await app._run_lite_mode("/lite-mode off")
        assert not NovaConfig().get_caveman_enabled()
        assert NovaConfig().get_caveman_level() == "lite"


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
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        button = app.query_one("#caveman-toggle", Button)
        assert button.region.width > 0
        assert button.region.height == 1
        assert button.region.right <= size[0]
        assert str(button.label) == "lite:off"
        assert await pilot.click("#caveman-toggle")
        await pilot.pause()
        assert NovaConfig().get_caveman_enabled()
        assert str(button.label) == "lite:caveman"
        await pilot.pause(0.4)  # Button ignores repeat presses during its active animation.
        button.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not NovaConfig().get_caveman_enabled()
        await app._run_lite_mode("/lite-mode on")
        await app._run_lite_mode("/lite-mode status")
        await app._run_lite_mode("/lite-mode invalid")
        assert NovaConfig().get_caveman_enabled()
    app = _app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert str(app.query_one("#caveman-toggle", Button).label) == "lite:caveman"


async def test_failed_save_keeps_current_toggle(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            NovaConfig, "set_compression_mode", Mock(side_effect=OSError("read only"))
        )
        await app._set_caveman_enabled(enabled=True)
        assert not app._caveman_enabled
        assert str(app.query_one("#caveman-toggle", Button).label) == "lite:off"


@pytest.mark.timeout(40)
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

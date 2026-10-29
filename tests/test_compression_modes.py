"""Exclusive compression modes and parity with the pinned Ponytail prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from novacode_cli.agents.caveman import CavemanMiddleware
from novacode_cli.config.nova_config import NovaConfig
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401


def test_legacy_preference_migrates_without_enabling_ponytail() -> None:
    config = NovaConfig()
    assert config.get_compression_mode() == "off"
    config.set("caveman_enabled", value=True)
    assert config.get_compression_mode() == "caveman"
    config.set_compression_mode("ponytail")
    assert not config.get_caveman_enabled()
    assert not any(config.get_caveman_features().values())
    assert NovaConfig().get_compression_mode() == "ponytail"
    config.set_caveman_enabled(True)
    assert config.get_compression_mode() == "caveman"
    config.set_compression_mode("off")
    assert not config.get_caveman_enabled()


@pytest.mark.parametrize("mode", [None, True, "invalid", "Caveman"])
def test_invalid_modes_cannot_partially_save(mode: object) -> None:
    config = NovaConfig()
    with pytest.raises(ValueError, match="compression mode"):
        config.set_compression_mode(mode)
    assert config.get_compression_mode() == "off"


def test_vendored_ponytail_files_match_upstream_checksums() -> None:
    from novacode_cli.agents.ponytail import PONYTAIL_ROOT

    manifest = json.loads((PONYTAIL_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert manifest["revision"] == "09c434ab73ca43761c5d8b0c5d341926c2fba710"
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((PONYTAIL_ROOT / relative).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("level", ["lite", "full", "ultra", "off", "review"])
def test_prompt_matches_upstream_javascript_builder(level: str) -> None:
    from novacode_cli.agents.ponytail import ponytail_instructions

    golden = Path(__file__).parent / "fixtures" / "ponytail_instructions.json"
    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert hashlib.sha256(ponytail_instructions(level).encode()).hexdigest() == expected[level]


@pytest.mark.parametrize("style_only", [False, True])
def test_ponytail_replaces_style_without_caveman_tool_compression(*, style_only: bool) -> None:
    from novacode_cli.agents.ponytail import ponytail_instructions

    config = NovaConfig()
    config.set_compression_mode("ponytail")
    config.set_ponytail_level("ultra")
    messages = [HumanMessage("Fix this"), ToolMessage("exact output\n" * 1000, tool_call_id="a")]
    original = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        system_message=SystemMessage("Keep existing permissions."),
        state={"messages": messages},
    )
    middleware = CavemanMiddleware(style_only=style_only)
    assert middleware.before_agent(original.state, None) == {"caveman_route": None}
    assert middleware.before_model(original.state, None) is None
    prepared = middleware._prepare_live(original)
    assert ponytail_instructions("ultra") in prepared.system_message.text
    assert "nova_caveman_style" not in prepared.system_message.text
    assert prepared.messages == messages
    assert original.system_message.text == "Keep existing permissions."
    config.set_compression_mode("off")
    assert CavemanMiddleware(style_only=style_only)._prepare_live(original) is original


@pytest.mark.parametrize("text", ["stop ponytail", " NORMAL MODE!!! ", "Stop Ponytail.? "])
def test_standalone_deactivation(text: str) -> None:
    from novacode_cli.agents.ponytail import is_deactivation_command

    assert is_deactivation_command(text)


@pytest.mark.parametrize(
    "text", ["add a normal mode toggle", "please stop ponytail", "normal mode; run x"]
)
def test_deactivation_never_matches_task_prose(text: str) -> None:
    from novacode_cli.agents.ponytail import is_deactivation_command

    assert not is_deactivation_command(text)


@pytest.mark.parametrize("name", ["review", "audit", "debt", "gain", "help"])
def test_helpers_preserve_upstream_body_and_mode(name: str) -> None:
    from novacode_cli.agents.ponytail import helper_prompt, skill_body

    config = NovaConfig()
    config.set_compression_mode("caveman")
    prompt = helper_prompt(f"/ponytail-{name} src/auth.py")
    assert prompt.startswith(skill_body("ponytail-" + name))
    assert "src/auth.py" in prompt
    assert config.get_compression_mode() == "caveman"


async def test_composer_toggles_preserving_draft_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from textual.widgets import Button, Select

    from tests.test_tui_home_preferences import _app

    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(45, 25)) as pilot:
        prompt = app.query_one("#prompt")
        prompt.text = "unfinished draft"
        button = app.query_one("#caveman-toggle", Button)
        for mode in ("caveman", "off"):
            await pilot.click("#caveman-toggle")
            await pilot.pause(0.4)
            assert NovaConfig().get_compression_mode() == mode
            assert prompt.text == "unfinished draft"
        await app._run_lite_mode("/lite-mode ponytail ultra")
        assert str(button.label) == "Lite: Ponytail"
        await app._run_lite_mode("/lite-mode on")
        assert NovaConfig().get_ponytail_level() == "ultra"
        await app._run_lite_mode("/lite-mode settings")
        await pilot.pause()
        assert app.screen.query_one("#compression-mode", Select).value == "ponytail"
        assert app.screen.query_one("#ponytail-level", Select).value == "ultra"
        await pilot.press("escape")
        await pilot.pause()
        assert NovaConfig().get_compression_mode() == "ponytail"


@pytest.mark.parametrize("saved", [None, True, [], {}, "Ponytail", "unknown"])
def test_malformed_explicit_mode_is_off(saved: object) -> None:
    config = NovaConfig()
    config.set("caveman_enabled", value=True)
    config.set_compression_mode("ponytail")
    config.set("compression_mode", saved)
    assert config.get_compression_mode() == "off"


def test_invalid_option_draft_never_partially_saves() -> None:
    config = NovaConfig()
    config.set_compression_mode("ponytail")
    features = dict.fromkeys(config.get_caveman_features(), False)
    with pytest.raises(ValueError, match="Ponytail intensity"):
        config.set_caveman_options(features, "lite", mode="off", ponytail_level="bad")
    assert NovaConfig().get_compression_mode() == "ponytail"
    assert NovaConfig().get_caveman_level() == "full"


async def test_shared_loop_deactivates_without_calling_agent() -> None:
    from unittest.mock import Mock

    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.ui_events import Done

    config = NovaConfig()
    config.set_compression_mode("ponytail")
    agent = Mock()
    events = [event async for event in iterate_agent_events("normal mode!", agent, None, Mock())]
    assert len(events) == 1
    assert isinstance(events[0], Done)
    assert NovaConfig().get_compression_mode() == "off"
    assert not agent.mock_calls


def test_live_code_checker_accepts_valid_function_and_rejects_io() -> None:
    from scripts.verify_compression_live import check_code

    assert check_code(
        "```python\ndef clamp(value, lower, upper):\n"
        '    if lower > upper:\n        raise ValueError(f"bad {lower}")\n'
        "    return min(max(value, lower), upper)\n```"
    )
    assert not check_code(
        "```python\nimport os\ndef clamp(value, lower, upper):\n"
        '    return os.system("echo unsafe")\n```'
    )
    assert not check_code("```python\ndef clamp(value, lower, upper):\n    return value\n```")


@pytest.mark.parametrize("name", ["review", "audit", "debt", "gain", "help"])
async def test_helper_command_dispatches_exact_skill_without_mode_change(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, Mock

    from novacode_cli.agents.ponytail import helper_prompt
    from tests.test_tui_home_preferences import _app

    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._stream_prompt = AsyncMock()
        app._add_message = AsyncMock()
        await app._run_slash(f"/ponytail-{name} src/auth.py")
        app._stream_prompt.assert_awaited_once_with(helper_prompt(f"/ponytail-{name} src/auth.py"))
        assert NovaConfig().get_compression_mode() == "off"


async def test_settings_save_works_in_narrow_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from textual.widgets import Button, Select

    from tests.test_tui_home_preferences import _app

    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(45, 25)) as pilot:
        await app._run_lite_mode("/lite-mode settings")
        await pilot.pause()
        app.screen.query_one("#compression-mode", Select).value = "ponytail"
        app.screen.query_one("#ponytail-level", Select).value = "lite"
        save = app.screen.query_one("#caveman-save", Button)
        save.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#caveman-save")
        await pilot.pause()
        assert NovaConfig().get_compression_mode() == "ponytail"
        assert NovaConfig().get_ponytail_level() == "lite"
        assert str(app.query_one("#caveman-toggle", Button).label) == "Lite: Ponytail"

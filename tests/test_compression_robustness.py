"""Regression cases discovered during the compression-mode code review."""

from unittest.mock import Mock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.agents.caveman import CavemanMiddleware
from novacode_cli.agents.caveman_support import _browse_compact, inspect_session, script_blocks
from novacode_cli.config.nova_config import NovaConfig
from tests.test_caveman_features import enable


def test_script_identity_includes_tool_and_all_execution_arguments():
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": tool, "args": {"command": "pytest -q", "cwd": cwd}, "id": str(i)}
                for i, (tool, cwd) in enumerate(
                    [("shell", "project-a"), ("shell", "project-b"), ("bash", "project-a")]
                )
            ],
        )
    ]
    blocks = script_blocks(messages)
    assert len(blocks) == 3
    for block in blocks:
        assert (
            inspect_session([], "recover", ref=block["reference"], saved_blocks=blocks)["args"]
            == block["args"]
        )


@pytest.mark.parametrize("fence", ["````", "~~~~"])
def test_browse_preserves_nested_fence_content_exactly(fence: str):
    code = (
        fence + "markdown\n```python\nvalue = 1\n\n\n\n```\n\n\n\nkeep these blank lines\n" + fence
    )
    assert code in _browse_compact("Intro\n\n\n" + code + "\n\n\nEnd")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["normal mode!", "Stop Caveman."])
async def test_caveman_deactivation_stops_before_agent_dispatch(command: str):
    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.ui_events import Done

    NovaConfig().set_compression_mode("caveman")
    agent = Mock()
    events = [e async for e in iterate_agent_events(command, agent, None, Mock())]
    assert len(events) == 1
    assert isinstance(events[0], Done)
    assert NovaConfig().get_compression_mode() == "off"
    assert not agent.mock_calls


def test_auto_route_does_not_override_model_after_tool_returns_an_image(
    monkeypatch: pytest.MonkeyPatch,
):
    enable("auto")
    original_model = FakeListChatModel(responses=["ok"])
    build = Mock(return_value=FakeListChatModel(responses=["text-only"]))
    monkeypatch.setattr("novacode_cli.agents.model_router.build_route_model", build)
    message = ToolMessage(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
        tool_call_id="image",
    )
    request = ModelRequest(
        model=original_model,
        messages=[message],
        state={"messages": [message], "caveman_route": {"id": "cheap"}},
        system_message=SystemMessage("Keep permissions"),
    )
    assert CavemanMiddleware()._prepare_live(request).model is original_model
    build.assert_not_called()


def test_legacy_script_references_remain_recoverable():
    from novacode_cli.agents.caveman_support import reference

    legacy = {
        "reference": reference("pytest"),
        "tool": "shell",
        "args": {"command": "pytest"},
        "uses": 1,
        "chars": 6,
    }
    assert (
        inspect_session([], "recover", ref=legacy["reference"], saved_blocks=[legacy])["args"]
        == legacy["args"]
    )


@pytest.mark.parametrize("mode", ["ponytail", "caveman"])
@pytest.mark.parametrize("level", ["lite", "full", "ultra"])
def test_compression_preserves_coding_contract_and_system_metadata(mode: str, level: str):
    from novacode_cli.prompts import render_template

    config = NovaConfig()
    config.set_compression_mode(mode)
    config.set_ponytail_level(level)
    config.set_caveman_level(level)
    for template in ("core_agent_system.jinja", "System_Prompt_Nova.jinja"):
        source = render_template(template)
        system = SystemMessage(content=source, id="policy", additional_kwargs={"scope": "test"})
        request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]),
            messages=[HumanMessage("Fix this")],
            system_message=system,
            state={"messages": []},
        )
        prepared = CavemanMiddleware(style_only=True)._prepare_live(request)
        for contract in (
            "observable acceptance conditions",
            "original defect",
            "real integration boundary",
            "completion status",
            "Do not weaken assertions",
            "atomic",
        ):
            assert contract in prepared.system_message.text
        assert prepared.system_message.id == "policy"
        assert prepared.system_message.additional_kwargs == {"scope": "test"}
        assert system.content == source
        if mode == "ponytail":
            assert (
                "not task scope, permissions, correctness or verification"
                in prepared.system_message.text
            )
            assert "repeated approval gates" in prepared.system_message.text


@pytest.mark.parametrize(
    ("mode", "command"),
    [
        ("caveman", "stop ponytail"),
        ("ponytail", "stop caveman"),
        ("off", "normal mode"),
        ("caveman", "implement normal mode"),
        ("ponytail", "normal mode; run tests"),
    ],
)
def test_mode_deactivation_does_not_consume_task_prose_or_other_mode_commands(
    mode: str, command: str
):
    from novacode_cli.agents.ponytail import is_deactivation_command

    assert not is_deactivation_command(command, mode)


@pytest.mark.asyncio
async def test_live_verifier_stops_when_provider_probe_fails(capsys: pytest.CaptureFixture):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scripts.verify_compression_live import main

    invoke = AsyncMock(side_effect=TimeoutError)
    assert not await main("test", "unavailable", SimpleNamespace(ainvoke=invoke))
    invoke.assert_awaited_once()
    assert "mode quality not evaluated" in capsys.readouterr().out

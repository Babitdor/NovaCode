"""Six independent features preserve exact data and normal execution boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from textual.widgets import Select, SelectionList

from novacode_cli.agents.caveman import CAVEMAN_INSTRUCTIONS, CavemanMiddleware
from novacode_cli.agents.caveman_support import (
    audit_messages,
    caveman_tool,
    compress_text,
    inspect_session,
    reference,
    script_blocks,
)
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.caveman_screen import CavemanScreen
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app

if TYPE_CHECKING:
    from langchain_core.outputs import ChatResult

FEATURES = ("say", "read", "auto", "blocks", "waste", "browse")


def enable(only: str | None = None):
    config = NovaConfig()
    config.set_caveman_enabled(True)
    config.set_caveman_features({name: only is None or name == only for name in FEATURES})


def prepare(messages: list):
    middleware = CavemanMiddleware()
    state = {"messages": messages}
    state.update(middleware.before_model(state, None))
    req = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        state=state,
        system_message=SystemMessage(content="Standing instructions"),
    )
    return middleware._prepare_live(req), state


@pytest.mark.parametrize("name", FEATURES)
def test_every_feature_has_an_independent_persistent_switch(name: str):
    enable(name)
    assert NovaConfig().get_caveman_features() == {key: key == name for key in FEATURES}
    NovaConfig().set_caveman_enabled(False)
    assert not any(NovaConfig().get_caveman_features().values())
    NovaConfig().set_caveman_enabled(True)
    assert NovaConfig().get_caveman_features()[name]


@pytest.mark.parametrize(
    ("text", "kwargs"),
    [
        (
            "progress unchanged\n" * 600
            + "FAIL tests/a.py:42\nAssertionError: x != 2\nexit code 1",
            {},
        ),
        (json.dumps({"path": "C:\\src\\a.py", "rows": list(range(500))}, indent=4), {}),
        ("key,value\n" + "".join(f"row{i},{i}\n" for i in range(600)), {"tabular": True}),
        (
            "<!doctype html><html><style>" + ".unused { color: red; }" * 500 + "</style>"
            '<h1>Guide</h1><a href="/docs">Docs</a><pre>x != 2\n  exact_indent</pre>'
            '<input name="query"></html>',
            {"browse": True},
        ),
        (
            json.dumps(
                {"status_code": 200, "content": "# Guide\n" + "\n" * 6000 + "[Docs](/docs)"}
            ),
            {"browse": True},
        ),
    ],
    ids=["logs", "json", "csv", "html", "fetch-envelope"],
)
def test_compressors_emit_recoverable_smaller_views(text: str, kwargs: dict[str, bool]):
    compact = compress_text(text, **kwargs)
    assert len(compact) < len(text) * 0.85
    assert reference(text) in compact
    recovered = []
    offset = 0
    while True:
        page = inspect_session(
            [],
            "recover",
            ref=reference(text),
            offset=offset,
            limit=1000,
            sources={reference(text): text},
        )
        recovered.append(page["content"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert "".join(recovered) == text
    if "AssertionError" in text:
        assert "AssertionError: x != 2" in compact
        assert "exit code 1" in compact
    if "<h1>" in text:
        assert "Guide" in compact
        assert "/docs" in compact
        assert "x != 2\n  exact_indent" in compact


def test_json_preserves_large_numbers_escaping_duplicate_keys_and_negation():
    text = (
        "{\n"
        + " " * 3000
        + '"n": 1.234567890123456789, "n": 99999999999999999999, "text":"not false \\""}'
    )
    compact = compress_text(text)
    assert "1.234567890123456789" in compact
    assert "99999999999999999999" in compact
    assert compact.count('"n":') == 2
    assert "not false" in compact


@pytest.mark.parametrize(
    "text",
    ["small", "{" + " " * 3000, "a\n" + "z" * 3000, "x" * 2_000_001],
    ids=["small", "malformed", "unknown", "oversize"],
)
def test_small_unknown_malformed_or_oversize_data_passes_through(text: str):
    assert compress_text(text) == text


def test_originals_and_tool_metadata_survive_model_view_compression():
    enable("read")
    text = "line\n" * 1000 + "ERROR exact failure\n"
    original = ToolMessage(content=text, name="execute", tool_call_id="one", status="error")
    image = HumanMessage(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    )
    prepared, state = prepare([image, original])
    assert len(prepared.messages[1].text) < len(text)
    assert prepared.messages[1].status == "error"
    assert prepared.messages[1].tool_call_id == "one"
    assert prepared.messages[0] == image
    assert original.text == text
    assert state["caveman_sources"][reference(text)] == text
    assert CAVEMAN_INSTRUCTIONS not in prepared.system_message.text
    assert inspect_session([], "recover", ref=reference(text))["error"]


def test_waste_fix_deduplicates_only_repeats_with_recovery():
    enable("waste")
    text = "unique result " * 150
    messages = [ToolMessage(content=text, name="custom", tool_call_id=str(i)) for i in range(2)]
    prepared, state = prepare(messages)
    assert prepared.messages[0].text == text
    assert "Repeated tool result" in prepared.messages[1].text
    assert reference(text) in state["caveman_sources"]
    report = audit_messages(messages)
    assert report["duplicate_tool_results"] == 1
    assert text not in json.dumps(report)


def test_audit_separates_provider_input_output_total_from_text_estimates():
    report = audit_messages([AIMessage(content="Short response", usage_metadata={
        "input_tokens": 120, "output_tokens": 20, "total_tokens": 140,
        "input_token_details": {"cache_read": 100},
        "output_token_details": {"reasoning": 12},
    })])
    assert report["reported_input_tokens"] == 120
    assert report["reported_output_tokens"] == 20
    assert report["reported_total_tokens"] == 140
    assert report["reported_reasoning_tokens"] == 12
    assert report["reported_cache_read_tokens"] == 100
    assert report["usage_observations"] == 1
    assert "estimated" in report["basis"]
    assert audit_messages([])["usage_observations"] == 0


def test_source_storage_failure_or_capacity_never_causes_lossy_compression():
    enable("read")
    text = "line\n" * 1000
    message = ToolMessage(content=text, name="execute", tool_call_id="one")
    req = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[message], state={})
    assert CavemanMiddleware()._prepare_live(req).messages[0] == message
    state = {"messages": [message], "caveman_sources": {"full": "z" * 8_000_000}}
    result = CavemanMiddleware().before_model(state, None)
    assert reference(text) not in result["caveman_sources"]


def test_scripts_are_checkpointed_exactly_and_cannot_execute_via_recovery():
    enable("blocks")
    command = 'python -c "print(42)"'
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": command, "timeout": 10},
                "id": "one",
            }
        ],
    )
    _, state = prepare([message])
    saved = state["caveman_blocks"]
    assert saved[0]["reference"] == reference(command)
    result = inspect_session([], "recover", ref=reference(command), saved_blocks=saved)
    assert result["args"] == {"command": command, "timeout": 10}
    assert result["requires_current_approval"] is True
    assert inspect_session([], "blocks", saved_blocks=saved)["blocks"]
    assert not script_blocks([HumanMessage(content=command)])
    assert "error" in inspect_session([], "recover", ref=reference(command))


def test_recovery_rejects_paths_and_bad_ranges():
    assert "error" in inspect_session([], "recover", ref="../../secret")
    assert "error" in inspect_session([], "recover", ref="C:\\secret")
    assert "error" in inspect_session([], "recover", ref="a" * 64, offset=-1)
    assert "error" in inspect_session([], "recover", ref="a" * 64, limit=12001)


def test_recovery_tool_can_read_archived_data_after_history_is_removed():
    text = "source\n" * 1000
    runtime = SimpleNamespace(state={"messages": [], "caveman_sources": {reference(text): text}})
    recovered = caveman_tool.func(action="recover", runtime=runtime, reference=reference(text))
    assert recovered["content"] == text
    assert "error" in caveman_tool.func(action="blocks", runtime=runtime)


def test_cache_audit_separates_observed_cache_tokens_from_estimates():
    report = audit_messages(
        [
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 2,
                    "total_tokens": 102,
                    "input_token_details": {"cache_read": 80},
                },
            )
        ]
    )
    assert report["reported_cache_read_tokens"] == 80
    assert report["reported_input_tokens"] == 100


def test_auto_uses_configured_routes_once_and_avoids_multimodal_downgrades(
    monkeypatch: pytest.MonkeyPatch,
):
    enable("auto")
    config = NovaConfig()
    route = {
        "id": "cheap",
        "provider": "ollama",
        "model": "test",
        "criteria": "Small asks",
        "reasoning_effort": "low",
    }
    config.set_router_routes([route])
    config.set_router_enabled(False)
    decide = Mock(return_value=SimpleNamespace(is_fallback=False, route_id="cheap"))
    monkeypatch.setattr("novacode_cli.agents.model_router.ModelRouter.decide", decide)
    middleware = CavemanMiddleware()
    update = middleware.before_agent({"messages": [HumanMessage(content="hi")]}, None)
    assert update["caveman_route"] == route
    assert decide.call_count == 1
    image = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:AA"}}])
    assert middleware.before_agent({"messages": [image]}, None)["caveman_route"] is None
    config.set_router_enabled(True)
    assert (
        middleware.before_agent({"messages": [HumanMessage(content="hi")]}, None)["caveman_route"]
        is None
    )
    assert decide.call_count == 1


async def test_options_save_and_cancel_are_persistent_and_independent(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    monkeypatch.setattr(type(app), "_sample_home_system", Mock())
    async with app.run_test(size=(80, 32)) as pilot:
        await pilot.click("#caveman-options")
        await pilot.pause()
        assert isinstance(app.screen, CavemanScreen)
        choices = app.screen.query_one(SelectionList)
        assert set(choices.selected) == set(FEATURES)
        choices.deselect("browse")
        await pilot.click("#caveman-save")
        await pilot.pause()
        assert NovaConfig().get("caveman_features")["browse"] is False
        assert NovaConfig().get_caveman_enabled() is False
        await app._run_lite_mode("/lite-mode settings")
        await pilot.pause()
        app.screen.query_one(SelectionList).select("browse")
        await pilot.press("escape")
        await pilot.pause()
        assert NovaConfig().get("caveman_features")["browse"] is False


async def test_router_effort_can_be_selected_and_persisted():
    from textual.widgets import Input

    from novacode_cli.tui.screens import RouterScreen

    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = RouterScreen(NovaConfig())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#router-id", Input).value = "cheap"
        screen.query_one("#router-provider", Select).value = "ollama"
        screen.query_one("#router-model", Input).value = "model"
        screen.query_one("#router-criteria", Input).value = "Small asks"
        screen.query_one("#router-effort", Select).value = "low"
        route = screen._read_fields()
        NovaConfig().set_router_routes([route])
        assert NovaConfig().get_router_routes()[0]["reasoning_effort"] == "low"


def test_route_cache_keeps_effort_distinct_and_does_not_change_global_config(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.agents.model_router import build_route_model

    config = NovaConfig()
    config.set("reasoning_effort", "medium")
    build = Mock(side_effect=lambda *_args, **_kwargs: object())
    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", build)
    route = {"provider": "ollama", "model": "caveman-cache-probe", "reasoning_effort": "low"}
    low = build_route_model(route)
    assert build_route_model(route) is low
    high = build_route_model({**route, "reasoning_effort": "high"})
    assert low is not high
    assert build.call_args.kwargs["reasoning_effort"] == "high"
    assert NovaConfig().get("reasoning_effort") == "medium"
    assert build_route_model({**route, "reasoning_effort": {}}) is None


async def test_compiled_tool_loop_recovers_exact_source_and_isolates_checkpoint_threads():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import RemoveMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from novacode_cli.security.prompt_injection import PromptInjectionMiddleware

    enable()
    source = "unchanged status\n" * 500 + "FAIL src/main.py:20\nexit code 1"
    seen = []

    class Model(GenericFakeChatModel):
        def bind_tools(self, *_args: object, **_kwargs: object) -> Model:
            return self

        def _generate(self, messages: list, *args: object, **kwargs: object) -> ChatResult:
            seen.append(messages)
            return super()._generate(messages, *args, **kwargs)

    @tool("execute")
    def test_output() -> str:
        """Return synthetic test output without executing any command."""
        return source

    model = Model(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "read"}]),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "caveman",
                            "args": {"action": "recover", "reference": reference(source)},
                            "id": "recover",
                        }
                    ],
                ),
                AIMessage(content="Recovered exact failure"),
            ]
        )
    )
    graph = create_agent(
        model,
        tools=[test_output, caveman_tool],
        middleware=[CavemanMiddleware(), PromptInjectionMiddleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "one"}}
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="inspect test output")]}, config
    )
    compact = next(m for m in seen[1] if isinstance(m, ToolMessage))
    assert "Compact view" in compact.text
    assert "FAIL src/main.py:20" in compact.text
    assert "UNTRUSTED TOOL DATA" in compact.text
    assert result["caveman_sources"][reference(source)] == source
    recovered = next(
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "caveman"
    )
    assert json.loads(recovered.text)["content"] == source
    await graph.aupdate_state(config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    state = (await graph.aget_state(config)).values
    assert (
        inspect_session([], "recover", ref=reference(source), sources=state["caveman_sources"])[
            "content"
        ]
        == source
    )
    other = (await graph.aget_state({"configurable": {"thread_id": "two"}})).values
    assert "error" in inspect_session(
        [], "recover", ref=reference(source), sources=other.get("caveman_sources")
    )


@pytest.mark.parametrize("path", ["C:\\repo\\values.json", "/repo/values.json", "/repo/source.py"])
def test_numbered_reads_compact_data_files_but_preserve_source_code(path: str):
    from deepagents.backends.utils import format_content_with_line_numbers

    enable("read")
    source = json.dumps({"values": list(range(600))}, indent=4)
    numbered = format_content_with_line_numbers(source)
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"file_path": path},
                "id": "file",
            }
        ],
    )
    result = ToolMessage(content=numbered, name="read_file", tool_call_id="file")
    prepared, state = prepare([call, result])
    if path.endswith(".py"):
        assert prepared.messages[-1].text == numbered
    else:
        assert len(prepared.messages[-1].text) < len(numbered)
        assert state["caveman_sources"][reference(numbered)] == numbered

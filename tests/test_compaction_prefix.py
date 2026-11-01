"""Summary forks retain assembled requests and never execute returned tools."""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables.config import var_child_runnable_config

from novacode_cli.agents.context_compaction import ContextCompactionMiddleware


def _request() -> ModelRequest:
    return ModelRequest(
        model=FakeListChatModel(responses=["fallback summary"]),
        messages=[
            HumanMessage("Keep constraints"),
            AIMessage("Completed a step"),
            HumanMessage("Next"),
        ],
        system_message=SystemMessage("System instructions and changing memory"),
        tools=[
            {
                "type": "function",
                "function": {"name": "work", "parameters": {"type": "object", "properties": {}}},
            }
        ],
        model_settings={"temperature": 0.1, "max_tokens": 512},
        state={},
    )


async def test_prefix_fork_keeps_system_tools_settings_history_and_hides_summary():
    request = _request()
    middleware = ContextCompactionMiddleware(context_window=8192)
    plan = middleware._plan(request, force=True)
    plan.cutoff = 2
    before = var_child_runnable_config.get()

    async def handler(fork: ModelRequest) -> ModelResponse:
        assert fork.model is request.model
        assert fork.system_message == request.system_message
        assert fork.tools == request.tools
        assert fork.model_settings == request.model_settings
        assert fork.messages[:-1] == request.messages
        assert "messages 1 through 2" in fork.messages[-1].content
        config = var_child_runnable_config.get()
        assert "nostream" in config["tags"]
        assert config["metadata"]["lc_source"] == "summarization"
        return ModelResponse(result=[AIMessage("Useful continuation summary")])

    assert await middleware._fork_summary(request, plan, handler) == "Useful continuation summary"
    assert var_child_runnable_config.get() is before
    assert len(request.messages) == 3


async def test_summary_range_tracks_messages_removed_during_shaping() -> None:
    request = _request().override(
        messages=[
            HumanMessage("original task"),
            AIMessage(""),
            HumanMessage("recent tail"),
        ]
    )
    middleware = ContextCompactionMiddleware(context_window=8192)
    middleware._snip = True
    plan = middleware._plan(request, force=True)
    plan.cutoff = 2

    async def handler(fork: ModelRequest) -> ModelResponse:
        assert len(fork.messages) == 3
        assert "messages 1 through 1" in fork.messages[-1].content
        assert fork.messages[-2].content == "recent tail"
        return ModelResponse(result=[AIMessage("summary")])

    assert await middleware._fork_summary(request, plan, handler) == "summary"


@pytest.mark.parametrize("reason", ["alternate", "reservation", "pending"])
async def test_ineligible_forks_keep_existing_summarizer_path(reason: str) -> None:
    request = _request()
    middleware = ContextCompactionMiddleware(context_window=8192)
    plan = middleware._plan(request, force=True)
    if reason == "alternate":
        middleware._model = FakeListChatModel(responses=["different route"])
    elif reason == "reservation":
        request = request.override(model_settings={"max_tokens": 8192})
    else:
        request = request.override(
            messages=[
                *request.messages,
                AIMessage("", tool_calls=[{"name": "work", "args": {}, "id": "pending"}]),
            ]
        )
        plan = middleware._plan(request, force=True)

    async def handler(_fork: ModelRequest) -> ModelResponse:
        pytest.fail("An ineligible prefix must not be dispatched")

    assert await middleware._fork_summary(request, plan, handler) is None


async def test_summary_tool_calls_are_discarded_without_execution():
    request = _request()
    middleware = ContextCompactionMiddleware(context_window=8192)
    plan = middleware._plan(request, force=True)
    plan.cutoff = 2

    async def handler(_fork: ModelRequest) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage("summary", tool_calls=[{"name": "work", "args": {}, "id": "unexpected"}])
            ]
        )

    assert await middleware._fork_summary(request, plan, handler) is None
    assert not any(isinstance(m, ToolMessage) for m in request.messages)


async def test_unexpected_fork_tools_fall_back_to_existing_summary_and_archive() -> None:
    request = _request()
    middleware = ContextCompactionMiddleware(context_window=8192)
    plan = middleware._plan(request, force=True)
    plan.cutoff = 2

    async def handler(_fork: ModelRequest) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    "",
                    tool_calls=[
                        {"name": "work", "args": {}, "id": "unexpected"},
                    ],
                )
            ]
        )

    messages, update = await middleware._compact(request, plan, overflow=False, handler=handler)
    assert "fallback summary" in messages[0].content
    assert messages[-1] == request.messages[-1]
    assert update["_summarization_event"]["file_path"]


async def test_legacy_summary_rejects_text_accompanied_by_tool_calls() -> None:
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    from novacode_cli.compaction import SummaryUnavailableError, _summary_call

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                "unsafe summary",
                tool_calls=[
                    {"name": "work", "args": {}, "id": "unexpected"},
                ],
            )
        ]
    )
    with pytest.raises(SummaryUnavailableError, match="tool calls"):
        await _summary_call(model, "Summarize", "summary.test")


async def test_fork_cancellation_resets_inherited_config():
    request = _request()
    middleware = ContextCompactionMiddleware(context_window=8192)
    plan = middleware._plan(request, force=True)
    plan.cutoff = 2
    before = var_child_runnable_config.get()

    async def handler(_fork: ModelRequest) -> ModelResponse:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware._fork_summary(request, plan, handler)
    assert var_child_runnable_config.get() is before


@pytest.mark.parametrize("entrypoint", ["graph", "subagents"])
def test_final_library_assembly_places_compaction_after_shaping_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    from deepagents import graph
    from deepagents.middleware import subagents
    from langchain.agents.middleware.types import AgentMiddleware

    import novacode_cli.agents.core_agent as core
    from novacode_cli.context.snapshot import ContextSnapshotMiddleware

    compactor = ContextCompactionMiddleware(context_window=8192)
    snapshot = ContextSnapshotMiddleware()
    memory = AgentMiddleware()
    captured = []

    def compile_agent(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return "compiled"

    monkeypatch.setattr(core, "_original_create_agent", compile_agent)
    target = graph if entrypoint == "graph" else subagents
    source = [compactor, snapshot, memory]
    assert target.create_agent("model", middleware=source) == "compiled"
    assert captured[0][1]["middleware"] == [memory, compactor, snapshot]
    assert source == [compactor, snapshot, memory]
    target.create_agent("model", middleware=[compactor, memory])
    assert captured[1][1]["middleware"] == [compactor, memory]


def test_real_harness_profile_exclusion_precedes_main_and_child_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
    from deepagents.middleware.subagents import SubAgentMiddleware
    from deepagents.profiles.harness import harness_profiles
    from langchain_openai import ChatOpenAI

    import novacode_cli.agents.core_agent as core
    from novacode_cli.context.snapshot import ContextSnapshotMiddleware

    monkeypatch.setattr(harness_profiles, "_HARNESS_PROFILES", {})
    register_harness_profile(
        "openai:nova-context-order-test",
        HarnessProfile(excluded_tools=frozenset({"execute"})),
    )
    model = ChatOpenAI(model="nova-context-order-test", api_key="test-only")
    captured = []
    original = core._original_create_agent

    def compile_agent(*args: object, **kwargs: object) -> object:
        captured.append(kwargs["middleware"])
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_original_create_agent", compile_agent)
    create_deep_agent(
        model=model,
        middleware=[ContextCompactionMiddleware(), ContextSnapshotMiddleware()],
        subagents=[
            {
                "name": "profile-child",
                "description": "Offline child",
                "system_prompt": "policy",
                "middleware": [ContextCompactionMiddleware(), ContextSnapshotMiddleware()],
            }
        ],
    )
    main = captured[0]
    child_owner = next(m for m in main if isinstance(m, SubAgentMiddleware))
    child = next(s for s in child_owner._subagents if s["name"] == "profile-child")
    core._original_create_sub_agent(child)
    assert len(captured) == 2
    for stack in captured:
        exclusion = next(i for i, m in enumerate(stack) if m.name == "_ToolExclusionMiddleware")
        compactor = next(
            i for i, m in enumerate(stack) if isinstance(m, ContextCompactionMiddleware)
        )
        snapshot = next(i for i, m in enumerate(stack) if isinstance(m, ContextSnapshotMiddleware))
        assert exclusion < compactor < snapshot
        assert snapshot == len(stack) - 1

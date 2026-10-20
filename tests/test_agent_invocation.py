"""Invocation routes and compiled-roster refresh use project definitions safely."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from novacode_cli.agents.invocation import (
    definition_signature,
    delegation_prompt,
    refresh_definitions,
)
from novacode_cli.config.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from novacode_cli import ui_events as ev


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, _tools: list, **_kwargs: object):
        return self


@pytest.fixture
def definitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    project, home = tmp_path / "project/.nova/agents", tmp_path / "home/.nova/agents"
    project.mkdir(parents=True)
    home.mkdir(parents=True)
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: project)
    monkeypatch.setattr(settings, "get_agents_root_dir", lambda: home)
    return project, home


def test_mixed_mentions_use_separate_tools_and_project_mode(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, home = definitions
    (home / "reviewer.md").write_text("---\nasync: only\n---\nGlobal", encoding="utf-8")
    (project / "reviewer.md").write_text("---\nasync: false\n---\nProject", encoding="utf-8")
    (project / "background.md").write_text("---\nasync: only\n---\nWorker", encoding="utf-8")
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_usable", lambda: True
    )
    prompt = delegation_prompt("Review then investigate", ["reviewer", "background"], settings)
    assert "`task(subagent_type='reviewer'" in prompt
    assert "`start_async_task(subagent_type='background'" in prompt
    assert "task ID is not a completed result" in prompt


def test_background_only_without_server_fails_clearly(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (definitions[0] / "worker.md").write_text("---\nasync: only\n---\nWork", encoding="utf-8")
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_usable", lambda: False
    )
    with pytest.raises(ValueError, match=r"background-only.*no agent server"):
        delegation_prompt("Investigate", ["worker"], settings)


def test_background_mentions_use_the_registered_lowercase_graph_id(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (definitions[0] / "Reviewer.md").write_text("---\nasync: only\n---\nReview", encoding="utf-8")
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_usable", lambda: True
    )
    assert "subagent_type='reviewer'" in delegation_prompt("Review", ["Reviewer"], settings)


async def test_refresh_detects_same_size_edits_and_does_not_rebuild_unchanged_graph(
    definitions: tuple[Path, Path],
) -> None:
    path = definitions[0] / "worker.md"
    path.write_text("Alpha", encoding="utf-8")
    graph = SimpleNamespace(agent_definitions_signature=definition_signature())
    backend = object()
    reload = AsyncMock(return_value=("new graph", "new backend"))
    state = SimpleNamespace(_model=object(), reload_agent_definitions=reload)
    assert await refresh_definitions(graph, backend, state) == (graph, backend)
    reload.assert_not_awaited()
    path.write_text("Bravo", encoding="utf-8")
    assert await refresh_definitions(graph, backend, state) == ("new graph", "new backend")
    reload.assert_awaited_once()


@pytest.mark.usefixtures("definitions")
async def test_reload_failure_never_runs_stale_tools() -> None:
    graph = SimpleNamespace(agent_definitions_signature="old")
    state = SimpleNamespace(
        _model=object(),
        reload_agent_definitions=AsyncMock(side_effect=RuntimeError("rebuild failed")),
    )
    with pytest.raises(RuntimeError, match="rebuild failed"):
        await refresh_definitions(graph, None, state)


async def test_explicit_teammate_snapshot_is_not_rebuilt() -> None:
    graph = SimpleNamespace(agent_definitions_signature=None)
    state = SimpleNamespace(_model=object(), reload_agent_definitions=AsyncMock())
    assert await refresh_definitions(graph, None, state) == (graph, None)
    state.reload_agent_definitions.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "delete", "override"])
def test_signature_changes_when_definition_membership_changes(
    definitions: tuple[Path, Path], operation: str
) -> None:
    project, home = definitions
    (home / "worker.md").write_text("Global worker", encoding="utf-8")
    before = definition_signature()
    if operation == "create":
        (project / "fresh.md").write_text("Fresh worker", encoding="utf-8")
    elif operation == "delete":
        (home / "worker.md").unlink()
    else:
        (project / "worker.md").write_text("Project worker", encoding="utf-8")
    assert definition_signature() != before


async def test_same_name_sync_and_background_are_both_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepagents.middleware import async_subagents

    from novacode_cli.agents.default_subagents.subagents import unique_subagent_specs

    child = ScriptedModel(responses=[AIMessage("SYNC_REVIEW")])
    model = ScriptedModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "id": "sync-call",
                        "name": "task",
                        "args": {
                            "subagent_type": "reviewer",
                            "description": "Review synchronously",
                        },
                    }
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    {
                        "id": "background-call",
                        "name": "start_async_task",
                        "args": {
                            "subagent_type": "reviewer",
                            "description": "Review in background",
                        },
                    }
                ],
            ),
            AIMessage("Both dispatched"),
        ]
    )
    client = SimpleNamespace(
        threads=SimpleNamespace(create=AsyncMock(return_value={"thread_id": "remote-thread"})),
        runs=SimpleNamespace(create=AsyncMock(return_value={"run_id": "remote-run"})),
    )
    monkeypatch.setattr(async_subagents._ClientCache, "get_async", lambda *_args: client)
    specs = unique_subagent_specs(
        [
            {"name": "reviewer", "description": "Sync", "system_prompt": "Review", "model": child},
            {
                "name": "reviewer",
                "description": "Async",
                "graph_id": "reviewer",
                "url": "http://unused",
            },
        ]
    )
    graph = create_deep_agent(model=model, tools=[], subagents=specs)
    result = await graph.ainvoke({"messages": [HumanMessage("Run both reviewers")]})
    outputs = [
        str(message.content) for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert any("SYNC_REVIEW" in content for content in outputs)
    assert any("task_id: remote-thread" in content for content in outputs)
    assert result["async_tasks"]["remote-thread"]["agent_name"] == "reviewer"
    assert client.runs.create.await_args.kwargs["assistant_id"] == "reviewer"


async def test_refresh_invokes_new_project_agent_without_losing_checkpoint_or_model(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli.agents.core_agent import build_named_subagents
    from novacode_cli.states.Session import SessionState

    child = ScriptedModel(responses=[AIMessage("NEW_PROJECT_AGENT")])
    model = ScriptedModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "id": "new-call",
                        "name": "task",
                        "args": {"subagent_type": "fresh", "description": "Review correction"},
                    }
                ],
            ),
            AIMessage("Done"),
        ]
    )
    monkeypatch.setattr("novacode_cli.config.model_create.build_dynamic_role_model", lambda: child)
    saver, store, backend = InMemorySaver(), InMemoryStore(), object()
    calls = []

    def build(**kwargs: object) -> tuple:
        calls.append(kwargs)
        graph = create_deep_agent(
            model=model,
            tools=[],
            subagents=build_named_subagents("nova-agent", []),
            checkpointer=saver,
        )
        graph.agent_definitions_signature = definition_signature()
        graph.agent_definitions_workspace = str(definitions[0].parents[1])
        return graph, backend

    original, _ = build()
    policy = object()
    original.agent_definition_options = {
        "system_prompt": "Custom root instructions",
        "extra_middleware": [policy],
        "exec_sandbox": True,
        "auto_approve": True,
    }
    state = SessionState()
    state.set_agent_context(original, backend, saver, store, [], "nova-agent", model)
    config = {"configurable": {"thread_id": state.thread_id}}
    await original.aupdate_state(config, {"messages": [HumanMessage("Correction: SQLite only.")]})
    (definitions[0] / "fresh.md").write_text(
        "---\ndescription: New project reviewer\n---\nRespect the correction.", encoding="utf-8"
    )
    monkeypatch.setattr("novacode_cli.agents.core_agent.create_agent_with_config", build)
    hook = AsyncMock()
    monkeypatch.setattr("novacode_cli.hooks.dispatch_hook_fire_and_forget", hook)
    refreshed, _ = await refresh_definitions(original, backend, state)
    assert calls[-1]["model"] is model
    assert calls[-1]["checkpointer"] is saver
    assert calls[-1]["store"] is store
    assert calls[-1]["steering_instructions"] == state.steering_instructions
    assert calls[-1]["workspace_root"] == str(definitions[0].parents[1])
    assert calls[-1]["system_prompt"] == "Custom root instructions"
    assert calls[-1]["extra_middleware"] == [policy]
    assert calls[-1]["exec_sandbox"] is True
    assert calls[-1]["auto_approve"] is True
    hook.assert_not_called()
    result = await refreshed.ainvoke({"messages": [HumanMessage("Use fresh")]}, config)
    assert any(message.content == "Correction: SQLite only." for message in result["messages"])
    assert any(
        isinstance(message, ToolMessage) and "NEW_PROJECT_AGENT" in str(message.content)
        for message in result["messages"]
    )


@pytest.mark.parametrize(
    "text", ["@background investigate", "@reviewer then @background investigate"]
)
async def test_tui_dispatch_routes_background_only_mentions(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    from novacode_cli.tui.app import NovaApp
    from tests.test_agents_screen import _app

    (definitions[0] / "background.md").write_text("---\nasync: only\n---\nWorker", encoding="utf-8")
    (definitions[0] / "reviewer.md").write_text(
        "---\ndescription: Review\n---\nReview", encoding="utf-8"
    )
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_usable", lambda: True
    )
    app = _app()
    stream = AsyncMock()
    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test(size=(100, 30)):
        await NovaApp._dispatch_root.__wrapped__(app, text)
    prompt = stream.await_args.args[0]
    assert "`start_async_task(subagent_type='background'" in prompt
    if "reviewer" in text:
        assert "`task(subagent_type='reviewer'" in prompt


async def test_tui_turn_refreshes_before_streaming(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli import ui_events as ev
    from tests.test_agents_screen import _app
    from tests.test_tui_app import _FakeAgent

    app = _app()
    app.agent.agent_definitions_signature = definition_signature()
    (definitions[0] / "fresh.md").write_text("New agent", encoding="utf-8")
    fresh, backend = _FakeAgent(), object()
    app.session_state._model = object()
    app.session_state.reload_agent_definitions = AsyncMock(return_value=(fresh, backend))
    seen = []

    async def stream(
        _text: str, graph: object, *_args: object, **kwargs: object
    ) -> AsyncIterator[ev.Done]:
        seen.append((graph, kwargs["backend"]))
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.tui.app.run_agent_stream", stream)
    async with app.run_test(size=(100, 30)):
        await app._do_stream("Use fresh")
    assert seen == [(fresh, backend)]
    assert app.agent is fresh


async def test_session_worker_refreshes_before_streaming(
    definitions: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli import ui_events as ev
    from tests.test_session_worker import _make

    seen = []

    async def stream(
        _text: str, graph: object, *_args: object, **kwargs: object
    ) -> AsyncIterator[ev.Done]:
        seen.append((graph, kwargs["backend"]))
        yield ev.Done()

    worker, _sent = _make(monkeypatch, stream)
    worker.agent = SimpleNamespace(agent_definitions_signature=definition_signature())
    (definitions[0] / "fresh.md").write_text("New agent", encoding="utf-8")
    fresh, backend = object(), object()
    worker.session_state._model = object()
    worker.session_state.reload_agent_definitions = AsyncMock(return_value=(fresh, backend))
    await worker._run_turn("fresh-prompt", "Use fresh")
    assert seen == [(fresh, backend)]
    assert worker.agent is fresh


def test_bound_graph_keeps_definition_version_and_workspace() -> None:
    from novacode_cli.session.checkpoint_coordinator import CoordinatedGraph

    graph = SimpleNamespace(with_config=lambda **_kwargs: object())
    original = CoordinatedGraph(graph, object())
    original.agent_definitions_signature = "definitions-v1"
    original.agent_definitions_workspace = "project-workspace"
    original.agent_definition_options = {"exec_sandbox": True, "extra_middleware": [object()]}
    bound = original.with_config(tags=["diagnostics"])
    assert bound.agent_definitions_signature == "definitions-v1"
    assert bound.agent_definitions_workspace == "project-workspace"
    assert bound.agent_definition_options == original.agent_definition_options

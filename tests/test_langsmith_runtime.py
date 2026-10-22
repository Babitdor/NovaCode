"""Runtime setup must reach actual LangGraph runs, not just environment values."""

import asyncio
import os

import langsmith
import pytest
from langsmith import utils

from novacode_cli.tracking import tracing


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch):
    saved = {name: os.environ.get(name) for name in tracing.LANGSMITH_ENV_VARS}
    old = tracing._tracing_config
    for name in tracing.LANGSMITH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    tracing.reset_langsmith_caches()
    client = langsmith.Client(
        api_url="https://example.test", api_key="fake", info={}, auto_batch_tracing=False
    )

    def reject_network(*_args, **_kwargs):
        pytest.fail("Runtime tracing tests must not contact LangSmith")

    monkeypatch.setattr(langsmith.Client, "request_with_retries", reject_network)
    monkeypatch.setattr(tracing, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(langsmith.Client, "create_run", lambda _self, **_kwargs: None)
    monkeypatch.setattr(langsmith.Client, "update_run", lambda _self, *_args, **_kwargs: None)
    yield client
    tracing._tracing_config = old
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tracing.reset_langsmith_caches()
    client.close()


def enable(project="runtime-test"):
    return tracing.apply_langsmith_env(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_API_KEY": "fake",
            "LANGSMITH_ENDPOINT": "https://example.test",
            "LANGSMITH_PROJECT": project,
        }
    )


async def test_setup_in_other_task_overrides_stale_disabled_context(isolated_tracing):
    async def source():
        assert utils.tracing_is_enabled() is True
        assert langsmith.get_tracing_context()["project_name"] == "runtime-test"
        assert langsmith.get_tracing_context()["client"] is isolated_tracing
        yield "event"

    with langsmith.tracing_context(enabled=False, project_name="old-project"):
        await asyncio.create_task(asyncio.to_thread(enable))
        assert not utils.tracing_is_enabled()
        assert [item async for item in tracing.traced_stream(source())] == ["event"]
        assert not utils.tracing_is_enabled()


async def test_disable_overrides_stale_enabled_context_and_closes_source():
    from novacode_cli.tracking.usage_tree import scoped_stream

    enable()
    closed = []

    async def source():
        try:
            assert not utils.tracing_is_enabled()
            yield "event"
        finally:
            closed.append(True)

    with langsmith.tracing_context(enabled=True):
        tracing.apply_langsmith_env({"LANGSMITH_TRACING": "false"})
        stream = scoped_stream(tracing.traced_stream(source()), "main")
        assert await anext(stream) == "event"
        await stream.aclose()
        assert utils.tracing_is_enabled()
    assert closed == [True]


def test_auto_configure_does_not_reenable_an_explicit_off():
    enable()
    tracing.apply_langsmith_env({"LANGSMITH_TRACING": "false"})
    assert not tracing.auto_configure().enabled


def test_client_uses_api_url_and_workspace(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing, "Client", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "workspace-test")
    enable()
    tracing.get_client()
    assert calls == [
        {"api_url": "https://example.test", "api_key": "fake", "workspace_id": "workspace-test"}
    ]


async def test_real_graph_records_chain_model_and_tool_then_stops_on_disable(monkeypatch):
    from langchain.agents import create_agent
    from langchain.tools import tool
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tracers.langchain import wait_for_all_tracers

    class ScriptedModel(FakeMessagesListChatModel):
        def bind_tools(self, _tools, **_kwargs):
            return self

    @tool
    def double(value: int) -> int:
        """Double an integer."""
        return value * 2

    recorded = []
    monkeypatch.setattr(
        langsmith.Client, "create_run", lambda _self, **kwargs: recorded.append(kwargs)
    )
    enable("graph-project")
    tracing.apply_langsmith_env(
        {
            "LANGSMITH_AGENT_ID": "novacode-cli",
            "LANGSMITH_AGENT_ENVIRONMENT": "production",
        }
    )
    graph = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "double",
                            "args": {"value": 2},
                            "id": "double-call",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="4"),
            ]
        ),
        tools=[double],
    )

    with langsmith.tracing_context(enabled=False):
        assert [
            item
            async for item in tracing.traced_stream(
                graph.astream({"messages": [("user", "double 2")]})
            )
        ]
    await asyncio.to_thread(wait_for_all_tracers)
    assert {run["run_type"] for run in recorded} >= {"chain", "llm", "tool"}
    assert all(run["session_name"] == "graph-project" for run in recorded)
    assert any(
        run.get("extra", {}).get("metadata", {}).get("agent_id") == "novacode-cli"
        for run in recorded
    )
    assert any(
        run.get("extra", {}).get("metadata", {}).get("agent_environment") == "production"
        for run in recorded
    )
    count = len(recorded)
    tracing.apply_langsmith_env({"LANGSMITH_TRACING": "false"})
    with langsmith.tracing_context(enabled=True):
        assert [
            item
            async for item in tracing.traced_stream(
                graph.astream({"messages": [("user", "double 2")]})
            )
        ]
    await asyncio.to_thread(wait_for_all_tracers)
    assert len(recorded) == count

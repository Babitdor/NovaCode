"""The general-purpose delegate is discoverable and runs on the async server."""

import runpy

from novacode_cli.agents.async_agents._config import shipped_graphs
from novacode_cli.agents.default_subagents import async_subagents
from novacode_cli.prompts import render_template


def test_async_general_purpose_is_registered_with_matching_graph(monkeypatch):
    monkeypatch.setattr(async_subagents, "async_agents_available", lambda: True)
    monkeypatch.setattr(async_subagents, "async_agents_see_workspace", lambda: True)
    monkeypatch.setattr(async_subagents, "_user_agent_specs", lambda: [])
    specs = async_subagents.retrieve_async_subagents()
    spec = next(spec for spec in specs if spec["name"] == "general-purpose-async")
    assert spec["graph_id"] == "general-purpose-async"
    assert "general-purpose-async" in shipped_graphs()
    assert len({spec["name"] for spec in specs}) == len(specs)


def test_async_general_purpose_graph_keeps_workspace_and_approval_controls(monkeypatch, tmp_path):
    import deepagents

    from novacode_cli.agents import async_workspace
    from novacode_cli.config import model_create

    captured = {}
    monkeypatch.setattr(async_workspace, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(model_create, "build_async_agent_model", lambda: object())
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **kwargs: captured.update(kwargs))
    namespace = runpy.run_path(str(shipped_graphs()["general-purpose-async"]))
    namespace["build_graph"]()
    assert captured["name"] == "general-purpose-async"
    assert str(tmp_path) in str(captured["backend"].cwd)
    names = {type(m).__name__ for m in captured["middleware"]}
    assert {"NovaModelRetryMiddleware", "DelegatedApprovalMiddleware", "ShellMiddleware"} <= names
    assert "parent" in captured["system_prompt"]


def test_prompt_prefers_async_general_work_and_yields_while_pending():
    for template in ("core_agent_system.jinja", "System_Prompt_Nova.jinja"):
        rendered = render_template(template, has_async_agents=True)
        assert "general-purpose-async" in rendered
        assert "return control to the user" in rendered
        assert "completion notification" in rendered
        unavailable = render_template(template, has_async_agents=False)
        assert "general-purpose-async" not in unavailable


async def test_general_purpose_graph_compiles_and_runs_without_external_provider(
    monkeypatch, tmp_path
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    from novacode_cli.agents import async_workspace
    from novacode_cli.config import model_create

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setattr(async_workspace, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        model_create, "build_async_agent_model",
        lambda: Model(responses=[AIMessage(content="Background work completed")]),
    )
    graph = runpy.run_path(str(shipped_graphs()["general-purpose-async"]))["graph"]
    result = await graph.ainvoke({"messages": [HumanMessage(content="Report completion")]})
    assert result["messages"][-1].content == "Background work completed"

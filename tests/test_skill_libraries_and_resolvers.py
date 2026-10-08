"""Specialist discovery and dynamically resolved tools stay isolated across runs."""

from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.backends import OptimizedFilesystemBackend
from novacode_cli.skills.libraries import library_names
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware
from novacode_cli.skills.runtime import pinned_message
from novacode_cli.skills.tool_resolver import inventory_resolver
from tests.test_skills_runtime import RecordingModel


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr("novacode_cli.skills.refreshing_middleware.prewarm", lambda: None)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    for name in ("review", "debug", "unrelated"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\nmetadata:\n  include_tools: integration\n---\nUse the tools carefully.",
            encoding="utf-8",
        )
    return OptimizedFilesystemBackend(root_dir=tmp_path, virtual_mode=True)


def test_policy_defaults_and_explicit_overrides():
    assert "systematic-debugging" in library_names("bug-investigation-agent")
    assert library_names("general-purpose-async") is None
    assert library_names("custom") == ()
    assert library_names("custom", {"skill_names": ["review", "review"]}) == ("review",)
    assert library_names("code-review-agent", {"skill_names": []}) == ()
    assert library_names("custom", {"skill_names": "*"}) is None
    with pytest.raises(ValueError, match="skill_names"):
        library_names("custom", {"skill_names": ["../secret"]})


def test_selective_discovery_does_not_read_other_skill_files(library, monkeypatch):
    downloaded = []
    original = library.download_files
    monkeypatch.setattr(
        library, "download_files", lambda paths: (downloaded.extend(paths), original(paths))[1]
    )
    middleware = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], library_names=["review"], watch_dirs=[library.cwd]
    )
    middleware.before_agent({}, None, {})
    assert [skill["name"] for skill in middleware._skills] == ["review"]
    assert downloaded == ["/review/SKILL.md"]
    denied, _ = middleware._skill_load("debug", SimpleNamespace(state={}, config={}))
    assert denied.startswith("Error:")
    assert "/debug/SKILL.md" not in middleware._skill_search("debug")


def test_library_caches_and_activation_are_separate(library):
    a = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], library_names=["review"], watch_dirs=[library.cwd]
    )
    b = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], library_names=["debug"], watch_dirs=[library.cwd]
    )
    a.before_agent({}, None, {})
    b.before_agent({}, None, {})
    assert a._disk_key() != b._disk_key()
    message = pinned_message(a._skills[0], (library.cwd / "review/SKILL.md").read_text())
    assert a._active_tools([message]) == {"integration"}
    assert b._active_tools([message]) == set()
    assert a._active_tools([]) == set()


@tool
def generated_tool() -> str:
    """An integration tool with an opaque generated name."""
    return "integration result"


generated_tool.name = "mcp_linear_list_issues_ab12"


def test_alias_group_is_visible_after_load_even_with_tool_search(library):
    resolver_calls = []

    def resolver(name, runtime):
        resolver_calls.append(name)
        return [generated_tool] if name == "integration" else []

    middleware = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], library_names=["review"], tools=resolver
    )
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "skills_load",
                        "args": {"name": "review"},
                        "id": "load",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    {"name": generated_tool.name, "args": {}, "id": "call", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    search = ToolSearchMiddleware()
    search._frequent = set()
    result = create_agent(model, middleware=[middleware, search]).invoke(
        {"messages": [HumanMessage("Review")]}
    )
    assert generated_tool.name not in model.bound[0]
    assert generated_tool.name in model.bound[1]
    assert any(
        isinstance(m, ToolMessage) and m.content == "integration result" for m in result["messages"]
    )
    assert len(resolver_calls) >= 3  # model binding and execution both resolve
    assert generated_tool.metadata is None  # ephemeral activation never mutates the inventory


@pytest.mark.asyncio
async def test_async_resolver_uses_each_runs_context(library):
    seen = []

    async def resolver(name, runtime):
        user_id = runtime.context["user_id"]
        seen.append(user_id)

        @tool("context_tool")
        def scoped_tool() -> str:
            """Read the integration for the current user."""
            return user_id

        return [scoped_tool] if name == "integration" else []

    middleware = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], library_names=["review"], tools=resolver
    )
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "context_tool", "args": {}, "id": "call", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    graph = create_agent(model, middleware=[middleware])
    for user_id in ("alice", "bob"):
        result = await graph.ainvoke(
            {"messages": [HumanMessage("$review")]}, context={"user_id": user_id}
        )
        outputs = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
        assert outputs == [user_id]
    assert "alice" in seen and "bob" in seen


def test_ordinary_registered_tool_wins_private_name_collision(library):
    @tool(generated_tool.name)
    def ordinary_tool() -> str:
        """The tool already registered on the agent."""
        return "ordinary result"

    middleware = RefreshingSkillsMiddleware(backend=library, sources=["/"], tools=[generated_tool])
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": generated_tool.name, "args": {}, "id": "call", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    result = create_agent(model, tools=[ordinary_tool], middleware=[middleware]).invoke(
        {"messages": [HumanMessage("Run the registered tool")]}
    )
    assert any(
        isinstance(m, ToolMessage) and m.content == "ordinary result" for m in result["messages"]
    )


def test_inventory_resolver_groups_and_ambiguous_aliases():
    metadata = [{"name": generated_tool.name, "server": "linear", "skill_alias": "list_issues"}]
    resolver = inventory_resolver([generated_tool], metadata)
    assert resolver("linear", None) == [generated_tool]
    assert resolver("list_issues", None) == [generated_tool]
    assert resolver("unknown", None) == []
    duplicate = generated_tool.model_copy(update={"name": "mcp_other_list_issues"})
    ambiguous = inventory_resolver(
        [generated_tool, duplicate],
        [*metadata, {"name": duplicate.name, "server": "other", "skill_alias": "list_issues"}],
    )
    assert ambiguous("list_issues", None) == []
    assert ambiguous("other", None) == [duplicate]


def test_resolver_is_not_called_before_activation_or_after_compaction(library):
    calls = []

    def resolver(name, runtime):
        calls.append(name)
        return [generated_tool]

    middleware = RefreshingSkillsMiddleware(backend=library, sources=["/"], tools=resolver)
    middleware.before_agent({}, None, {})
    assert middleware._resolve_tools([HumanMessage("Review")], None) == {}
    message = pinned_message(middleware._skills[0], (library.cwd / "review/SKILL.md").read_text())
    assert generated_tool.name in middleware._resolve_tools([message], None)
    assert middleware._resolve_tools([HumanMessage("Summary of earlier work")], None) == {}
    assert calls == ["integration"]


def test_guessed_dynamic_tool_is_unknown_without_activation(library):
    calls = []

    def resolver(name, runtime):
        calls.append(name)
        return [generated_tool]

    middleware = RefreshingSkillsMiddleware(backend=library, sources=["/"], tools=resolver)
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": generated_tool.name, "args": {}, "id": "guess", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    result = create_agent(model, middleware=[middleware]).invoke(
        {"messages": [HumanMessage("Review")]}
    )
    outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert outputs[0].status == "error"
    assert calls == []


def test_sync_run_rejects_async_resolver_clearly(library):
    async def resolver(name, runtime):
        return [generated_tool]

    middleware = RefreshingSkillsMiddleware(backend=library, sources=["/"], tools=resolver)
    model = RecordingModel(responses=[AIMessage("Done")])
    with pytest.raises(RuntimeError, match="ainvoke or astream"):
        create_agent(model, middleware=[middleware]).invoke({"messages": [HumanMessage("$review")]})


def test_hardening_builds_fresh_selected_loaders(library):
    from novacode_cli.agents.core_agent import _harden_subagent_specs
    from novacode_cli.skills.refreshing_middleware import SubagentSkillsMiddleware

    specs = [
        {"name": "reviewer", "system_prompt": "Review", "skill_names": ["review"]},
        {"name": "debugger", "system_prompt": "Debug", "skill_names": ["debug"]},
        {"name": "general-purpose", "system_prompt": "Help"},
    ]
    first = _harden_subagent_specs(specs, ["/"], skill_backend=library)
    second = _harden_subagent_specs(specs, ["/"], skill_backend=library)
    for index, names in enumerate(({"review"}, {"debug"}, {"review", "debug", "unrelated"})):
        loaders = [m for m in first[index]["middleware"] if isinstance(m, SubagentSkillsMiddleware)]
        assert len(loaders) == 1
        assert first[index]["skills"] == []
        loaders[0].before_agent({}, None, {})
        assert {s["name"] for s in loaders[0]._skills} == names
        other = next(
            m for m in second[index]["middleware"] if isinstance(m, SubagentSkillsMiddleware)
        )
        assert other is not loaders[0]
    assert all("middleware" not in spec and "skills" not in spec for spec in specs)


def test_parent_activation_does_not_activate_child_tools(library):
    from deepagents import create_deep_agent

    parent = RefreshingSkillsMiddleware(
        backend=library, sources=["/"], tools=lambda name, runtime: [generated_tool]
    )
    child = RefreshingSkillsMiddleware(
        backend=library,
        sources=["/"],
        library_names=["review"],
        tools=lambda name, runtime: [generated_tool],
    )
    parent_model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"subagent_type": "reviewer", "description": "Review the code"},
                        "id": "delegate",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage("Done"),
        ]
    )
    child_model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": generated_tool.name, "args": {}, "id": "guess", "type": "tool_call"}
                ],
            ),
            AIMessage("Finished review"),
        ]
    )
    graph = create_deep_agent(
        parent_model,
        backend=library,
        middleware=[parent],
        subagents=[
            {
                "name": "reviewer",
                "description": "Review code",
                "system_prompt": "Review",
                "model": child_model,
                "middleware": [child],
            }
        ],
    )
    graph.invoke({"messages": [HumanMessage("Use $review then delegate a review")]})
    assert generated_tool.name in parent_model.bound[0]
    assert child_model.bound
    assert all(generated_tool.name not in names for names in child_model.bound)

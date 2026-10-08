"""End-to-end contracts for progressive skill loading and activation."""

from types import SimpleNamespace

import pytest
import yaml
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from novacode_cli.backends import OptimizedFilesystemBackend
from novacode_cli.commands.skill_invoke import _get_supporting_files
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware
from novacode_cli.skills.runtime import active_skill_names, pinned_message, request_reload
from novacode_cli.skills.schema import normalize_skill_frontmatter


@pytest.fixture
def skill(tmp_path, monkeypatch):
    monkeypatch.setattr("novacode_cli.skills.refreshing_middleware.prewarm", lambda: None)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    directory = tmp_path / "review"
    directory.mkdir()
    content = "---\nname: review\ndescription: Review code\nmetadata:\n  include_tools: private_review\n---\n\nInspect then verify.\n"
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    backend = OptimizedFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    middleware = RefreshingSkillsMiddleware(backend=backend, sources=["/"], watch_dirs=[tmp_path])
    middleware.before_agent({}, None, {})
    return middleware, directory, content


def test_pin_in_message_loads_body_once_and_not_references(skill):
    middleware, directory, _ = skill
    (directory / "reference.md").write_text("PRIVATE REFERENCE", encoding="utf-8")
    result = middleware.before_agent(
        {"messages": [HumanMessage("Use $review and /review on this code")]}, None, {}
    )
    pins = [m for m in result["messages"] if m.additional_kwargs.get("lc_source") == "pinned_skill"]
    assert len(pins) == 1
    assert "Inspect then verify." in pins[0].content
    assert "PRIVATE REFERENCE" not in pins[0].content
    assert "include_tools:" not in pins[0].content
    assert pins[0].additional_kwargs["skill"]["include_tools"] == ["private_review"]
    assert "skills_metadata" not in result


def test_explicit_pin_input_consumed_and_disabled_skills_skipped(skill, monkeypatch):
    middleware, _, _ = skill
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: {"review"})
    result = middleware.before_agent(
        {"pinned_skills": ["review", "missing"], "messages": [HumanMessage("hello")]}, None, {}
    )
    assert result == {"pinned_skills": []}


def test_snapshot_stays_unchanged_after_edit_and_repin_gets_new_body(skill):
    middleware, directory, _ = skill
    state = {"messages": [HumanMessage("$review")]}
    first = middleware.before_agent(state, None, {})["messages"][-1]
    path = directory / "SKILL.md"
    path.write_text(
        path.read_text().replace("Inspect then verify.", "New instructions."), encoding="utf-8"
    )
    second = middleware.before_agent(state, None, {})["messages"][-1]
    assert "Inspect then verify." in first.content
    assert "New instructions." in second.content


def test_reload_refreshes_even_without_watch_dirs(skill):
    middleware, directory, _ = skill
    middleware._watch_dirs = []
    path = directory / "SKILL.md"
    path.write_text(path.read_text().replace("Review code", "New description"), encoding="utf-8")
    request_reload()
    middleware.before_agent({}, None, {})
    assert middleware._skills[0]["description"] == "New description"


def test_source_cache_key_preserves_precedence(skill):
    middleware, _, _ = skill
    middleware.sources = ["/a/", "/b/"]
    first = middleware._disk_key()
    middleware.sources.reverse()
    assert middleware._disk_key() != first


def test_successful_read_activates_but_error_and_compaction_do_not(skill):
    middleware, _, content = skill
    metadata = middleware._skills
    ai = AIMessage(
        "",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"file_path": metadata[0]["path"]},
                "id": "read",
                "type": "tool_call",
            }
        ],
    )
    good = ToolMessage(content=content, tool_call_id="read", name="read_file")
    bad = ToolMessage(content="Error: denied", tool_call_id="read", status="error")
    assert active_skill_names([ai, good], metadata) == {"review"}
    assert active_skill_names([ai, bad], metadata) == set()
    assert active_skill_names([AIMessage("Compacted summary")], metadata) == set()


@pytest.mark.parametrize(
    "path", ["../secret", "references/../../secret", "/secret", "C:\\secret", "..\\secret", ""]
)
def test_resources_reject_escape_paths(skill, path):
    middleware, _, _ = skill
    runtime = SimpleNamespace(state={}, config={})
    assert middleware._read_resource("review", path, runtime).startswith("Error:")


def test_resource_read_is_paged_and_does_not_execute(skill):
    middleware, directory, _ = skill
    (directory / "reference.md").write_text(
        "\n".join(f"line {i}" for i in range(500)), encoding="utf-8"
    )
    runtime = SimpleNamespace(state={}, config={})
    result = middleware._read_resource("review", "reference.md", runtime, offset=0, limit=3)
    assert "line 0" in result and "line 499" not in result


def test_inventory_never_reads_contents_and_has_a_limit(tmp_path, monkeypatch):
    for i in range(150):
        (tmp_path / f"reference-{i}.md").write_text("private reference")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "secret.md").write_text("ignore")
    monkeypatch.setattr(
        type(tmp_path), "read_text", lambda *a, **k: pytest.fail("eager resource read")
    )
    inventory = _get_supporting_files(tmp_path)
    assert len(inventory) == 100
    assert set(inventory.values()) == {""}
    assert all("node_modules" not in name for name in inventory)


def test_normalization_preserves_nested_metadata_multiline_yaml():
    content = "---\nname: review\ndescription: |\n  Review code\n  carefully.\nmetadata:\n  include_tools: private_review\n  owner:\n    team: nova\ntags:\n  - code\n---\nBody"
    normalized = normalize_skill_frontmatter(content, "review")
    before = yaml.safe_load(content.split("---")[1])
    after = yaml.safe_load(normalized.split("---")[1])
    assert all(after[key] == value for key, value in before.items())
    assert normalize_skill_frontmatter(normalized, "review") == normalized


@tool
def private_review() -> str:
    """Run the skill's private review tool."""
    return "REVIEW COMPLETE"


class RecordingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        self.bound.append([getattr(t, "name", "") for t in tools])
        return self

    bound: list[list[str]] = []


def test_real_graph_gates_private_tool_and_activates_after_explicit_load(skill):
    previous, directory, _ = skill
    middleware = RefreshingSkillsMiddleware(
        backend=previous._backend,
        sources=["/"],
        watch_dirs=[directory.parent],
        tools=[private_review],
    )
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "private_review", "args": {}, "id": "denied", "type": "tool_call"}
                ],
            ),
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
                    {"name": "private_review", "args": {}, "id": "allowed", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    agent = create_agent(model, middleware=[middleware])
    result = agent.invoke({"messages": [HumanMessage("Please review")]})
    assert "private_review" not in model.bound[0]
    assert "private_review" in model.bound[2]
    outputs = {m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)}
    assert outputs["denied"].status == "error"
    assert outputs["allowed"].content == "REVIEW COMPLETE"
    assert outputs["load"].artifact["skill"]["name"] == "review"


@pytest.mark.asyncio
async def test_async_graph_pins_skills_and_runs_tools(skill):
    previous, directory, _ = skill
    middleware = RefreshingSkillsMiddleware(
        backend=previous._backend,
        sources=["/"],
        watch_dirs=[directory.parent],
        tools=[private_review],
    )
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "private_review", "args": {}, "id": "allowed", "type": "tool_call"}
                ],
            ),
            AIMessage("Done"),
        ]
    )
    result = await create_agent(model, middleware=[middleware]).ainvoke(
        {"messages": [HumanMessage("$review")], "pinned_skills": ["review"]}
    )
    assert "private_review" in model.bound[0]
    assert result["pinned_skills"] == []
    assert any(
        isinstance(m, ToolMessage) and m.content == "REVIEW COMPLETE" for m in result["messages"]
    )


def test_compaction_removes_private_tool_activation(skill):
    middleware, _, content = skill
    message = pinned_message(middleware._skills[0], content)
    assert middleware._active_tools([message]) == {"private_review"}
    assert middleware._active_tools([]) == set()


def test_arbitrary_tool_artifacts_cannot_activate_private_tools(skill):
    middleware, _, content = skill
    snapshot = pinned_message(middleware._skills[0], content).additional_kwargs["skill"]
    result = ToolMessage(
        content="something",
        name="unrelated_mcp_tool",
        tool_call_id="mcp",
        artifact={"skill": snapshot},
    )
    assert middleware._active_tools([result]) == set()


def test_metadata_reset_requests_a_reload_and_is_consumed(skill):
    middleware, directory, _ = skill
    middleware._watch_dirs = []
    path = directory / "SKILL.md"
    path.write_text(path.read_text().replace("Review code", "Reset description"), encoding="utf-8")
    result = middleware.before_agent({"skills_metadata": None}, None, {})
    assert result == {"skills_metadata": []}
    assert middleware._skills[0]["description"] == "Reset description"


def test_skills_tools_load_registered_optional_schemas(skill):
    from langchain.agents.middleware.types import ModelRequest

    from novacode_cli.agents.tool_search import ToolSearchMiddleware

    middleware, _, content = skill
    search = ToolSearchMiddleware(skill_tools=middleware._active_tools)
    search._frequent = set()
    messages = [pinned_message(middleware._skills[0], content)]
    request = ModelRequest(
        model=None,
        messages=messages,
        tools=[private_review],
        state={"messages": messages},
        runtime=None,
    )
    assert private_review in search._apply(request).tools
    request = request.override(messages=[], state={"messages": []})
    assert private_review not in search._apply(request).tools


def test_discovery_and_lookup_choose_same_source(tmp_path, monkeypatch):
    from novacode_cli.config.config import Settings
    from novacode_cli.skills.load import find_skill_dir, list_skills

    directories = [
        tmp_path / source for source in ("user", "shared", "claude", "plugin", "project")
    ]
    for directory in directories:
        path = directory / "review"
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: review\ndescription: {directory.name}\n---\nBody", encoding="utf-8"
        )
    user, shared, claude, plugin, project = directories
    fake = SimpleNamespace(
        get_global_skills_dir=lambda: user, get_project_skills_dirs=lambda: [project]
    )
    monkeypatch.setattr(Settings, "from_environment", lambda: fake)
    monkeypatch.setattr(Settings, "get_shared_skills_dir", lambda: shared)
    monkeypatch.setattr(Settings, "get_global_claude_skills_dir", lambda: claude)
    monkeypatch.setattr(
        "novacode_cli.plugins.claude_plugins.plugin_skill_dirs", lambda: [("test", plugin)]
    )
    discovered = list_skills(
        user_skills_dir=user,
        shared_skills_dir=shared,
        claude_skills_dir=claude,
        plugin_skills_dirs=[plugin],
        project_skills_dirs=[project],
    )
    assert find_skill_dir("review") == (project / "review", "project")
    assert discovered[0]["path"] == str(project / "review" / "SKILL.md")
    assert discovered[0]["description"] == "project"
    for name in ("../review", "review/../../secret", "C:/secret"):
        assert find_skill_dir(name) is None


def test_disabled_slash_invocations_do_not_read_or_run(monkeypatch):
    import novacode_cli.commands.skill_invoke as invoke
    from tests.test_skill_invoke import _FakeSettings

    monkeypatch.setattr(invoke, "Settings", _FakeSettings)
    monkeypatch.setattr(invoke, "list_skills", lambda **k: [{"name": "review"}])
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: {"review"})
    monkeypatch.setattr(
        invoke, "_read_skill_content", lambda *a: pytest.fail("disabled skill read")
    )
    assert invoke._resolve_skill_invocation("review", "--run", None, "test") is None

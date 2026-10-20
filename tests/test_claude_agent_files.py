"""Claude Code agent files (``.nova/agents/<name>.md``) as Nova subagents."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from pathlib import Path

APPROVER = """---
name: approver
description: Factory final gate. Writes decision.md.
tools: Read, Grep, Glob, Bash, Write
model: opus
---
You are the final gate for one factory job.
"""


@pytest.fixture
def agent_dirs(monkeypatch, tmp_path):
    from novacode_cli.config.config import settings

    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(settings, "get_agents_root_dir", lambda: home)
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: project)
    return home, project


ROLE = SimpleNamespace(name="dynamic-role-model")


@pytest.fixture
def role_model(monkeypatch):
    """The configured Dynamic role (falling back to Subagents) resolves to ROLE."""
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_dynamic_role_model", lambda: ROLE
    )
    built = []
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, name: built.append((provider, name)) or (provider, name),
    )
    return built


def test_flat_file_is_discovered_and_a_directory_wins(agent_dirs):
    from novacode_cli.config.config import agent_md, settings

    home, project = agent_dirs
    (project / "approver.md").write_text(APPROVER, encoding="utf-8")
    (home / "approver.md").write_text(APPROVER, encoding="utf-8")
    (home / "approver").mkdir()
    (home / "approver" / "agent.md").write_text(APPROVER, encoding="utf-8")

    found = {(name, scope): path for name, path, scope in settings.get_all_agents()}
    assert found[("approver", "project")] == project / "approver.md"
    assert found[("approver", "global")] == home / "approver"
    assert agent_md(found[("approver", "global")]) == home / "approver" / "agent.md"
    assert settings.find_agent("approver") == (project / "approver.md", "project")
    assert settings.find_agent("nobody") is None


def test_approver_becomes_a_restricted_subagent(agent_dirs, role_model):
    from novacode_cli.agents.core_agent import build_named_subagents

    (agent_dirs[1] / "approver.md").write_text(APPROVER, encoding="utf-8")
    tools = [SimpleNamespace(name=n) for n in ("web_search", "fetch_url")]

    (spec,) = [s for s in build_named_subagents("nova-agent", tools) if s["name"] == "approver"]
    assert spec["system_prompt"].startswith("You are the final gate")
    assert spec["description"] == "[project] Factory final gate. Writes decision.md."
    assert spec["tools"] == []
    assert spec["_nova_fs_allow"] == {"read_file", "grep", "glob", "execute", "write_file"}
    # `model: opus` runs on the configured subagent model, not a hard-coded Claude.
    assert spec["model"] is ROLE
    assert role_model == []


@pytest.mark.parametrize("written", ["", "opus", "sonnet", "haiku", "inherit", "gpt-5"])
def test_claude_model_values_follow_the_configured_role(written, role_model):
    from novacode_cli.agents.core_agent import _frontmatter_model

    assert _frontmatter_model("a", written) is ROLE
    assert role_model == []


def test_explicit_provider_model_still_wins(role_model):
    from novacode_cli.agents.core_agent import _frontmatter_model

    assert _frontmatter_model("a", "openai:gpt-5") == ("openai", "gpt-5")


def test_unbuildable_model_falls_back_to_the_role(monkeypatch, role_model):
    from novacode_cli.agents.core_agent import _frontmatter_model

    def boom(*_a):
        raise ValueError("no key")

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", boom)
    assert _frontmatter_model("a", "openai:gpt-5") is ROLE


def test_async_agent_files_run_on_the_async_role(agent_dirs, monkeypatch):
    """A background agent's graph ignores `model:`; the server's Async role decides."""
    from novacode_cli.agents import user_async_agents

    captured = {}
    monkeypatch.setattr(
        "novacode_cli.agents.async_agents._specialist.build_specialist_graph",
        lambda name, spec, **_k: captured.update(spec) or "graph",
    )
    path = agent_dirs[1] / "approver.md"
    path.write_text(APPROVER.replace("model: opus", "model: opus\nasync: true"), encoding="utf-8")
    assert user_async_agents.build_user_graph("approver", path) == "graph"
    assert "model" not in captured


def test_filter_hides_and_refuses_unlisted_filesystem_tools():
    from novacode_cli.agents.agent_file import (
        FilesystemToolFilter,
        agent_tools,
        filesystem_allowlist,
    )

    assert filesystem_allowlist(agent_tools(APPROVER)) == {
        "read_file", "grep", "glob", "execute", "write_file",
    }
    # Nova's own lists never name file tools, so they keep all of them.
    assert filesystem_allowlist(["web_search"]) is None
    assert filesystem_allowlist(None) is None

    mw = FilesystemToolFilter({"read_file", "write_file"})
    request = SimpleNamespace(
        tools=[SimpleNamespace(name=n) for n in ("read_file", "edit_file", "ls", "web_search")],
        override=lambda tools: SimpleNamespace(tools=tools),
    )
    seen = mw.wrap_model_call(request, lambda r: [t.name for t in r.tools])
    assert seen == ["read_file", "web_search"]

    call = SimpleNamespace(tool_call={"name": "edit_file", "id": "1"})
    refused = mw.wrap_tool_call(call, lambda _r: "ran")
    assert isinstance(refused, ToolMessage)
    assert refused.status == "error"
    ok = SimpleNamespace(tool_call={"name": "read_file", "id": "2"})
    assert mw.wrap_tool_call(ok, lambda _r: "ran") == "ran"


def test_harden_adds_the_filter_only_for_an_allowlist():
    from novacode_cli.agents.agent_file import FilesystemToolFilter
    from novacode_cli.agents.core_agent import _harden_subagent_specs

    restricted = {"name": "approver", "system_prompt": "x", "tools": [],
                  "_nova_fs_allow": {"read_file"}}
    plain = {"name": "plain", "system_prompt": "x", "tools": []}
    out = _harden_subagent_specs([restricted, plain])
    kinds = [[type(m) for m in spec["middleware"]] for spec in out]
    assert FilesystemToolFilter in kinds[0]
    assert FilesystemToolFilter not in kinds[1]
    assert "_nova_fs_allow" not in out[0]
    assert "_nova_fs_allow" in restricted  # the cached spec is never mutated


def test_a_project_agent_overrides_its_global_namesake(agent_dirs, monkeypatch):
    """Everywhere: lookup, the in-process roster and the background server."""
    from novacode_cli.agents.core_agent import build_named_subagents
    from novacode_cli.agents.default_subagents.subagents import unique_subagent_specs
    from novacode_cli.agents.user_async_agents import collect_user_async_agents
    from novacode_cli.config.config import settings

    home, project = agent_dirs
    for root, who in ((home, "GLOBAL"), (project, "PROJECT")):
        (root / "shared.md").write_text(
            f"---\ndescription: {who}\nasync: true\n---\nYou are {who}.\n", encoding="utf-8"
        )
    monkeypatch.setattr("novacode_cli.config.model_create.build_dynamic_role_model", lambda: None)

    assert settings.find_agent("shared") == (project / "shared.md", "project")
    (spec,) = unique_subagent_specs(build_named_subagents("nova-agent", []))
    assert spec["description"] == "[project] PROJECT"
    assert collect_user_async_agents() == [("shared", project / "shared.md")]


@pytest.mark.parametrize("project_flag", ["false", "only"])
def test_project_scope_wins_before_execution_mode_filtering(
    agent_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, project_flag: str
) -> None:
    """A project definition cannot accidentally expose its shadowed global agent."""
    from novacode_cli.agents.core_agent import build_named_subagents
    from novacode_cli.agents.user_async_agents import collect_user_async_agents

    home, project = agent_dirs
    (home / "shared.md").write_text(
        "---\ndescription: GLOBAL\nasync: true\n---\nGlobal prompt.\n", encoding="utf-8"
    )
    (project / "shared.md").write_text(
        f"---\ndescription: PROJECT\nasync: {project_flag}\n---\nProject prompt.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("novacode_cli.config.model_create.build_dynamic_role_model", lambda: None)
    specs = build_named_subagents("nova-agent", [])
    if project_flag == "only":
        assert specs == []
        assert collect_user_async_agents() == [("shared", project / "shared.md")]
    else:
        assert [spec["description"] for spec in specs] == ["[project] PROJECT"]
        assert collect_user_async_agents() == []

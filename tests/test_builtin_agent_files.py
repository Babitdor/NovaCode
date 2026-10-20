"""Nova's own specialists, shipped as agent files and seeded into ~/.nova/agents/."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.agents.agent_file import agent_tools, read_agent
from novacode_cli.agents.user_async_agents import is_async, is_background_only
from novacode_cli.config.config import BUILTIN_AGENTS_DIR, seed_builtin_agents

SHIPPED = sorted(f.stem for f in BUILTIN_AGENTS_DIR.glob("*.md"))
BACKGROUND = [n for n in SHIPPED if is_background_only(read_agent(BUILTIN_AGENTS_DIR / f"{n}.md")[0])]


def test_every_shipped_file_is_a_complete_agent():
    assert len(SHIPPED) == 14
    assert len(BACKGROUND) == 10
    for name in SHIPPED:
        front, body = read_agent(BUILTIN_AGENTS_DIR / f"{name}.md")
        assert front["description"].strip(), name
        assert body.strip(), name
        assert isinstance(front["skill_names"], list), name


def test_seeding_writes_once_and_a_deleted_file_stays_deleted(tmp_path):
    agents = tmp_path / "agents"
    assert sorted(seed_builtin_agents(agents)) == SHIPPED
    assert sorted(f.stem for f in agents.glob("*.md")) == SHIPPED

    (agents / "bug-fix-agent.md").unlink()
    (agents / "code-review-agent.md").write_text("mine", encoding="utf-8")
    assert seed_builtin_agents(agents) == []
    assert not (agents / "bug-fix-agent.md").exists()
    assert (agents / "code-review-agent.md").read_text(encoding="utf-8") == "mine"


def test_an_agent_the_user_already_has_is_kept(tmp_path):
    agents = tmp_path / "agents"
    (agents / "code-explorer").mkdir(parents=True)
    (agents / "code-explorer" / "agent.md").write_text("mine", encoding="utf-8")

    written = seed_builtin_agents(agents)
    assert "code-explorer" not in written
    assert not (agents / "code-explorer.md").exists()
    assert "code-explorer" in (agents / ".builtin-seeded").read_text(encoding="utf-8").split()


def test_background_only_agents_are_not_in_process_subagents(monkeypatch, tmp_path):
    from novacode_cli.agents.core_agent import build_named_subagents
    from novacode_cli.config.config import settings

    agents = tmp_path / "agents"
    seed_builtin_agents(agents)
    monkeypatch.setattr(settings, "get_agents_root_dir", lambda: agents)
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: None)
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_dynamic_role_model", lambda: None
    )

    names = {s["name"] for s in build_named_subagents("nova-agent", [])}
    assert names == set(SHIPPED) - set(BACKGROUND)
    assert is_async({"async": "only"})  # still served by the agent server


@pytest.mark.parametrize("name", BACKGROUND)
def test_each_background_agent_builds_with_its_tools(name, monkeypatch):
    """Through the same path the agent server uses for a user's async agent."""
    from novacode_cli.agents import user_async_agents
    from novacode_cli.agents.async_agents import _specialist

    built = {}
    monkeypatch.setattr(_specialist, "build_async_agent_model", lambda: SimpleNamespace())
    monkeypatch.setattr(
        _specialist.deepagents, "create_deep_agent", lambda **kw: built.update(kw) or "graph"
    )
    path = BUILTIN_AGENTS_DIR / f"{name}.md"

    assert user_async_agents.build_user_graph(name, path) == "graph"
    declared = agent_tools(path.read_text(encoding="utf-8")) or []
    assert sorted(t.name for t in built["tools"]) == sorted(declared), "a declared tool was not found"
    assert read_agent(path)[1].strip() in built["system_prompt"]

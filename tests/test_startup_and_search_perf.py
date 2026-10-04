"""Start-up and per-turn costs that were measured and removed.

Each test pins one behaviour whose absence cost seconds:

* subagent graphs are compiled on first use, not at agent-build time
  (91 graphs, 3.3 s of a 5.5 s build);
* a path-less grep/glob searches the project, not Nova's skill and memory
  stores (25,000 extra files; 0.14 s -> 1.06 s warm, a 10 s timeout cold);
* the parsed skill list survives a restart (the first model call of a session
  used to wait 2.4-10 s for ~1,000 SKILL.md files to be re-parsed).
"""

from __future__ import annotations

import pytest

# ── lazy subagent compilation ───────────────────────────────────────────────


class _Graph:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def invoke(self, state, config=None):  # noqa: ANN001, ANN201
        self.calls.append(("invoke", state))
        return {"messages": ["done"]}

    async def ainvoke(self, state, config=None):  # noqa: ANN001, ANN201
        self.calls.append(("ainvoke", state))
        return {"messages": ["done"]}

    def with_config(self, **kw):  # noqa: ANN003, ANN201
        return ("configured", kw)


@pytest.fixture
def builds(monkeypatch):
    import novacode_cli.agents.core_agent as ca

    made: list[str] = []

    def fake_create(spec, *, state_schema=None, response_format=None):  # noqa: ANN001, ANN202
        made.append(spec["name"])
        return _Graph()

    monkeypatch.setattr(ca, "_original_create_sub_agent", fake_create)
    return ca, made


def test_a_subagent_is_not_compiled_until_it_is_used(builds):
    ca, made = builds
    specs = [{"name": f"agent-{i}"} for i in range(5)]
    runnables = [ca._cached_create_sub_agent(s, state_schema=dict) for s in specs]
    assert made == [], "building the agent must not compile any subagent"

    assert runnables[2].invoke({"x": 1}) == {"messages": ["done"]}
    assert made == ["agent-2"], "only the one that was called"
    runnables[2].invoke({"x": 2})
    assert made == ["agent-2"], "compiled once, then reused"


def test_the_second_compile_pass_reuses_the_same_lazy_graph(builds):
    """deepagents builds every spec twice; the pair must stay one object."""
    ca, _ = builds
    spec = {"name": "reviewer"}
    assert ca._cached_create_sub_agent(spec, state_schema=dict) is ca._cached_create_sub_agent(
        spec, state_schema=dict
    )


def test_a_lazy_subagent_delegates_everything_else_to_the_real_graph(builds):
    import asyncio

    ca, made = builds
    lazy = ca._cached_create_sub_agent({"name": "explorer"}, state_schema=dict)
    with pytest.raises(AttributeError):
        lazy.__deepcopy__  # noqa: B018 — a dunder probe must not trigger a compile
    assert made == []
    assert lazy.with_config(tags=["t"]) == ("configured", {"tags": ["t"]})
    assert asyncio.run(lazy.ainvoke({"y": 1})) == {"messages": ["done"]}
    assert made == ["explorer"]


def test_a_requested_response_format_still_compiles_eagerly(builds):
    """That path bypasses the cache: the caller needs the real graph now."""
    ca, made = builds
    ca._cached_create_sub_agent({"name": "structured"}, state_schema=dict, response_format=object())
    assert made == ["structured"]


# ── path-less search stays in the project ───────────────────────────────────


def test_a_root_search_does_not_fan_out_into_skills_or_memory(tmp_path):
    from novacode_cli.agents.core_agent import _PROJECT_SEARCH_ROUTES, _build_composite_backend

    agent_dir = tmp_path / "agent"
    skills = tmp_path / "skills"
    ws = tmp_path / "ws"
    for d in (agent_dir / "memories", skills / "some-skill", ws):
        d.mkdir(parents=True)
    (skills / "some-skill" / "pyproject.toml").write_text("x = 1", encoding="utf-8")
    (agent_dir / "memories" / "note.toml").write_text("x = 1", encoding="utf-8")
    (ws / "pyproject.toml").write_text("x = 1", encoding="utf-8")

    composite, _ = _build_composite_backend(
        sandbox=None, sandbox_type=None, workspace_root=ws, skills_dir=skills,
        claude_skills_dir=tmp_path / "absent", project_skills_dirs=[], plugin_skills=[],
        agent_dir=agent_dir, store=None, assistant_id="a",
    )
    assert set(composite.routes) <= _PROJECT_SEARCH_ROUTES

    found = {m["path"] for m in composite.glob("*.toml", "/").matches}
    assert found == {"/pyproject.toml"}, found

    # ...while an explicit prefix still reaches the store.
    in_skills = {m["path"] for m in composite.glob("*.toml", "/skills/").matches}
    assert in_skills == {"/skills/some-skill/pyproject.toml"}
    assert composite.read("/memories/memories/note.toml").error is None


# ── the parsed skill list survives a restart ────────────────────────────────


def _skills(n: int) -> list[dict]:
    return [{"name": f"s{i}", "description": "d", "path": f"/skills/s{i}/SKILL.md"} for i in range(n)]


def test_the_skill_listing_round_trips_through_the_disk_cache(tmp_path, monkeypatch):
    from novacode_cli.config import config as cfg
    from novacode_cli.skills import refreshing_middleware as rm

    monkeypatch.setattr(cfg, "HOME_DIR", tmp_path)
    assert rm._disk_get("k") is None
    rm._disk_put("k", _skills(60))
    assert rm._disk_get("k") == _skills(60)

    rm._disk_put("tiny", _skills(3))
    assert rm._disk_get("tiny") is None, "small trees are not worth an entry"

    for i in range(rm._DISK_CACHE_ENTRIES + 2):
        rm._disk_put(f"k{i}", _skills(60))
    assert rm._disk_get("k") is None, "bounded: the oldest entries are dropped"


def test_a_fresh_process_loads_skills_from_disk_without_parsing(tmp_path, monkeypatch):
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.skills import SkillsMiddleware

    from novacode_cli.config import config as cfg
    from novacode_cli.skills import refreshing_middleware as rm

    monkeypatch.setattr(cfg, "HOME_DIR", tmp_path / "home")
    skills_dir = tmp_path / "skills"
    (skills_dir / "alpha").mkdir(parents=True)
    (skills_dir / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    def make():
        return rm.RefreshingSkillsMiddleware(
            backend=FilesystemBackend(root_dir=str(skills_dir), virtual_mode=True),
            sources=["/"],
            watch_dirs=[skills_dir],
        )

    first = make()
    first._skills_changed()
    # Seed the cache as an earlier process would have (bypassing the size floor).
    key = first._disk_key()
    path = rm._disk_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps({key: _skills(2)}), encoding="utf-8")

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("an unchanged skill tree must not be re-parsed after a restart")

    monkeypatch.setattr(SkillsMiddleware, "before_agent", boom)
    fresh = make()
    fresh.before_agent({}, None, None)
    assert {s["name"] for s in fresh._skills} == {"s0", "s1"}


def test_a_changed_skill_file_invalidates_the_disk_cache(tmp_path, monkeypatch):
    import os

    from deepagents.backends import FilesystemBackend

    from novacode_cli.config import config as cfg
    from novacode_cli.skills import refreshing_middleware as rm

    monkeypatch.setattr(cfg, "HOME_DIR", tmp_path / "home")
    skills_dir = tmp_path / "skills"
    (skills_dir / "alpha").mkdir(parents=True)
    md = skills_dir / "alpha" / "SKILL.md"
    md.write_text("---\nname: alpha\ndescription: d\n---\nbody\n", encoding="utf-8")
    mw = rm.RefreshingSkillsMiddleware(
        backend=FilesystemBackend(root_dir=str(skills_dir), virtual_mode=True),
        sources=["/"],
        watch_dirs=[skills_dir],
    )
    mw._skills_changed()
    before = mw._disk_key()
    os.utime(md, (1_000_000_000, 1_000_000_000))
    mw._skills_changed()
    assert mw._disk_key() != before, "an edited SKILL.md must miss the cache"

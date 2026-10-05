"""User-created async subagents: the flag, discovery, and the generated server config.

The config assertions are the load-bearing ones. ``langgraph_api`` decides
whether a graph spec is a file path or a module name with ``if "/" in
path_or_module``, so a Windows path written with backslashes is handed to
``importlib.import_module`` and fails on a file that plainly exists. That is a
silent failure on the user's machine and an invisible one in CI, so it is
asserted directly rather than left to a manual server run.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from novacode_cli.agents import user_async_agents as ua
from novacode_cli.agents.agent_file import read_agent, write_agent
from novacode_cli.agents.async_agents import _config

PROMPT = "You check javascript in the repo. Report findings with file and line."


def _only(*pairs: tuple[str, Path, str]):
    """A get_all_agents stub returning (name, dir, scope) for each (name, md, scope)."""
    return lambda: [(name, path.parent, scope) for name, path, scope in pairs]


def _load(module_path: Path, stem: str):
    """Load a generated module the way langgraph_api loads a graph spec."""
    spec = importlib.util.spec_from_file_location(stem, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _a_model(monkeypatch):
    """A model the graphs can resolve without reaching a provider."""
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "ollama")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "llama3")


# ── the flag ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "front", [{}, {"async": False}, {"async": "false"}, {"async": "no"}, {"async": None}]
)
def test_an_agent_without_the_flag_is_not_async(front):
    assert ua.is_async(front) is False


@pytest.mark.parametrize(
    "front",
    [{"async": True}, {"async": "true"}, {"async": "TRUE"}, {"async": "yes"}, {"async": 1}],
)
def test_the_flag_is_read_the_way_a_person_writes_it(front):
    assert ua.is_async(front) is True


def test_the_flag_survives_a_round_trip_through_the_file(tmp_path):
    """Written by the create modal as a bool, read back as one."""
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    front, body = read_agent(agent_md)
    assert front["async"] is True
    assert ua.is_async(front) is True
    assert body.strip() == PROMPT


# ── discovery ───────────────────────────────────────────────────────────────


def test_only_the_flagged_agents_are_collected(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings

    a = tmp_path / "a" / "agent.md"
    b = tmp_path / "b" / "agent.md"
    write_agent(a, {"description": "a", "async": True}, PROMPT)
    write_agent(b, {"description": "b"}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("a", a, "global"), ("b", b, "global")))

    assert ua.collect_user_async_agents() == [("a", a)]


def test_an_unreadable_agent_is_skipped_not_fatal(tmp_path, monkeypatch):
    """One broken file must not stop the session from serving the others."""
    from novacode_cli.config.config import settings

    good = tmp_path / "good" / "agent.md"
    write_agent(good, {"description": "g", "async": True}, PROMPT)
    missing = tmp_path / "missing" / "agent.md"  # never written
    monkeypatch.setattr(
        settings,
        "get_all_agents",
        _only(("good", good, "global"), ("missing", missing, "global")),
    )

    # read_agent_safe turns the read error into empty frontmatter, which is not
    # an async flag, so the agent is simply absent from the list.
    assert ua.collect_user_async_agents() == [("good", good)]


def test_an_agent_named_after_a_shipped_graph_is_refused():
    with pytest.raises(ValueError, match="collides"):
        ua.graph_id_for("code-review-agent")


@pytest.mark.parametrize("name", ["", "has space", "sla/sh", "../escape", "dot.name"])
def test_a_name_that_is_not_a_graph_id_is_refused(name):
    with pytest.raises(ValueError):
        ua.graph_id_for(name)


def test_the_protected_names_come_from_the_shipped_config_not_a_hardcoded_list():
    """A shipped agent added to langgraph.json is protected by name automatically."""
    graphs = json.loads(_config.package_config_path().read_text(encoding="utf-8"))["graphs"]
    assert set(graphs) == set(ua.shipped_graph_ids())


# ── the generated config ────────────────────────────────────────────────────


def test_every_generated_path_is_posix_because_the_server_treats_a_backslash_as_a_module(
    tmp_path,
):
    """The whole point: langgraph_api splits file-vs-module on ``/``."""
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)

    config_path, _names = _config.generate([("jeva", agent_md)], tmp_path / "gen")
    graphs = json.loads(config_path.read_text(encoding="utf-8"))["graphs"]

    assert graphs, "no graphs were generated"
    for graph_id, spec in graphs.items():
        path = spec.rsplit(":", 1)[0]
        assert "/" in path, f"{graph_id}: {path!r} has no '/', so it is imported as a module"
        assert "\\" not in path, f"{graph_id}: {path!r} is a Windows path the server cannot load"
        assert Path(path).is_file(), f"{graph_id}: {path!r} does not exist"


def test_the_generated_config_registers_the_shipped_graphs_too(tmp_path):
    """The shipped nine live in the package, so the generated config must not lose them."""
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)

    _config_path, names = _config.generate([("jeva", agent_md)], tmp_path / "gen")
    assert "jeva" in names
    assert set(ua.shipped_graph_ids()) <= set(names), "a shipped graph went missing"


def test_generation_is_byte_identical_for_the_same_agents(tmp_path):
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    args = ([("jeva", agent_md)], tmp_path / "gen")

    first = _config.generate(*args)[0].read_bytes()
    second = _config.generate(*args)[0].read_bytes()
    assert first == second


def test_the_user_prompt_is_read_from_the_file_not_inlined(tmp_path):
    """The generated module holds a path; the prompt is read inside the server.

    This is what lets the user edit their agent.md and have the next server pick
    it up, and it keeps arbitrary prompt text out of generated source.
    """
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    _config.generate([("jeva", agent_md)], tmp_path / "gen")

    source = (tmp_path / "gen" / "agent_jeva.py").read_text(encoding="utf-8")
    assert "You check javascript" not in source, "the prompt was inlined into generated code"
    # repr(), so the path appears escaped on Windows — which is also why a
    # backslash-free assertion is not needed for the module source (only for the
    # langgraph.json paths, which are read as data rather than imported).
    assert repr(str(agent_md)) in source, "the module must point at the agent's own file"
    assert "build_user_graph" in source


# ── the graph itself ────────────────────────────────────────────────────────


def test_a_generated_module_loads_and_exposes_a_real_graph(tmp_path):
    """Exactly what langgraph_api does with a graph spec: load the file, take the attr."""
    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    _config.generate([("jeva", agent_md)], tmp_path / "gen")

    module = _load(tmp_path / "gen" / "agent_jeva.py", "agent_jeva_under_test")

    assert module.graph is not None, "the graph must build from an agent.md with a prompt"
    assert hasattr(module.graph, "invoke")


def test_an_agent_with_no_prompt_yields_a_none_graph_rather_than_killing_the_server(tmp_path):
    """One bad user agent must not take the other graphs down with it."""
    agent_md = tmp_path / "empty" / "agent.md"
    write_agent(agent_md, {"description": "no prompt", "async": True}, "")
    _config.generate([("empty", agent_md)], tmp_path / "gen")

    module = _load(tmp_path / "gen" / "agent_empty.py", "agent_empty_under_test")

    assert module.graph is None
    with pytest.raises(ValueError, match="no system prompt"):
        ua.build_user_graph("empty", agent_md)


def test_the_graph_honours_the_tools_the_agent_chose(tmp_path, monkeypatch):
    agent_md = tmp_path / "picky" / "agent.md"
    write_agent(agent_md, {"description": "d", "tools": ["web_search"], "async": True}, PROMPT)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "novacode_cli.agents.async_agents._specialist.build_specialist_graph",
        lambda name, definition: seen.update(definition) or name,
    )

    ua.build_user_graph("picky", agent_md)
    assert seen["prompt"] == PROMPT
    assert seen["tools"] == ["web_search"]


def test_no_tools_key_means_no_named_tools_rather_than_a_lookup_error(tmp_path, monkeypatch):
    agent_md = tmp_path / "open" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "novacode_cli.agents.async_agents._specialist.build_specialist_graph",
        lambda name, definition: seen.update(definition) or name,
    )

    ua.build_user_graph("open", agent_md)
    assert seen["tools"] == []


# ── the specs the main agent sees ───────────────────────────────────────────


def test_a_user_async_agent_is_offered_as_a_spec(tmp_path, monkeypatch):
    from novacode_cli.agents.default_subagents import async_subagents as mod
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "Checks javascript", "async": True}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("jeva", agent_md, "global")))

    specs = mod._user_agent_specs()

    assert [s["name"] for s in specs] == ["jeva"]
    assert specs[0]["graph_id"] == "jeva", "the graph id is what the user typed"
    assert "Checks javascript" in specs[0]["description"]
    assert "background" in specs[0]["description"], "the main agent must know it is not inline"
    assert specs[0]["url"].startswith("http")


def test_an_ordinary_agent_is_never_offered_as_a_spec(tmp_path, monkeypatch):
    from novacode_cli.agents.default_subagents import async_subagents as mod
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "plain" / "agent.md"
    write_agent(agent_md, {"description": "Plain"}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("plain", agent_md, "global")))

    assert mod._user_agent_specs() == []


def test_an_agent_named_after_a_shipped_graph_is_not_offered(tmp_path, monkeypatch):
    """Silently shadowing a shipped graph would make the user's agent look absent."""
    from novacode_cli.agents.default_subagents import async_subagents as mod
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "security-audit-agent" / "agent.md"
    write_agent(agent_md, {"description": "Mine", "async": True}, PROMPT)
    monkeypatch.setattr(
        settings, "get_all_agents", _only(("security-audit-agent", agent_md, "global"))
    )

    assert mod._user_agent_specs() == []


def test_the_shipped_count_is_unchanged_when_the_user_has_no_async_agents(tmp_path, monkeypatch):
    """The 9 built-ins must still be exactly 9 for a user with nothing new."""
    import socket

    from novacode_cli.agents.default_subagents import async_subagents as mod
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "plain" / "agent.md"
    write_agent(agent_md, {"description": "Plain"}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("plain", agent_md, "global")))

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{srv.getsockname()[1]}")
        monkeypatch.setenv(mod.ASYNC_AGENT_ROOT_VAR, str(settings.get_workspace_root()))
        monkeypatch.setattr(mod, "_availability", None, raising=False)
        assert len(mod.retrieve_async_subagents()) == 9


def test_a_user_async_agent_is_added_to_the_full_spec_list(tmp_path, monkeypatch):
    """End to end through the real entry point, with the server answering."""
    import socket

    from novacode_cli.agents.default_subagents import async_subagents as mod
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "Checks javascript", "async": True}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("jeva", agent_md, "global")))

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{srv.getsockname()[1]}")
        monkeypatch.setenv(mod.ASYNC_AGENT_ROOT_VAR, str(settings.get_workspace_root()))
        monkeypatch.setattr(mod, "_availability", None, raising=False)
        specs = mod.retrieve_async_subagents()

    assert len(specs) == 10, "nine built-ins plus the user's agent"
    assert specs[-1]["name"] == "jeva"
    assert specs[-1]["graph_id"] == "jeva"


# ── the launcher ────────────────────────────────────────────────────────────


def test_no_user_agents_means_the_shipped_config_is_used_untouched(tmp_path, monkeypatch):
    """The common case must not change which config file the server is given."""
    from novacode_cli.agents import server_launcher as sl
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "plain" / "agent.md"
    write_agent(agent_md, {"description": "Plain"}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("plain", agent_md, "global")))

    assert sl._user_graph_config() is None
    assert sl._config_path() == _config.package_config_path()


def test_a_user_agent_makes_the_launcher_serve_a_generated_config(tmp_path, monkeypatch):
    from novacode_cli.agents import server_launcher as sl
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "Checks javascript", "async": True}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("jeva", agent_md, "global")))

    try:
        generated = sl._user_graph_config()
        assert generated is not None
        config_path, names = generated
        assert config_path != _config.package_config_path(), "must not touch the package's config"
        assert config_path.is_file()
        assert "jeva" in names
        # cwd is where the server runs from, so the generated modules must be there.
        assert config_path.parent == Path(config_path.parent)
        assert (config_path.parent / "agent_jeva.py").is_file()
    finally:
        sl.cleanup_user_config()


def test_the_generated_directory_is_removed_on_cleanup(tmp_path, monkeypatch):
    from novacode_cli.agents import server_launcher as sl
    from novacode_cli.config.config import settings

    agent_md = tmp_path / "jeva" / "agent.md"
    write_agent(agent_md, {"description": "d", "async": True}, PROMPT)
    monkeypatch.setattr(settings, "get_all_agents", _only(("jeva", agent_md, "global")))

    config_path, _names = sl._user_graph_config()
    directory = config_path.parent
    assert directory.is_dir()
    sl.cleanup_user_config()
    assert not directory.exists(), "the generated config outlived the session"

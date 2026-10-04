"""Nova's own agent server: planned at boot, started by the first dispatch,
and working in the session's project rather than in Nova's repo."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace


def test_only_a_dispatch_starts_the_server(monkeypatch):
    from novacode_cli.agents import server_launcher
    from novacode_cli.agents.async_on_demand import AsyncServerOnDemandMiddleware

    starts: list[int] = []

    async def ensure_started(**_):  # noqa: ANN003, ANN202
        starts.append(1)
        return True

    monkeypatch.setattr(server_launcher, "ensure_started", ensure_started)

    async def handler(request):  # noqa: ANN001, ANN202
        return "ran"

    async def call(name: str):  # noqa: ANN202
        request = SimpleNamespace(tool_call={"name": name, "id": "c1"})
        return await AsyncServerOnDemandMiddleware().awrap_tool_call(request, handler)

    assert asyncio.run(call("read_file")) == "ran" and starts == []
    assert asyncio.run(call("check_async_task")) == "ran" and starts == [], "checking must not launch"
    assert asyncio.run(call("start_async_task")) == "ran" and starts == [1]


def test_a_failed_launch_tells_the_agent_to_delegate_in_process(monkeypatch):
    from novacode_cli.agents import server_launcher
    from novacode_cli.agents.async_on_demand import AsyncServerOnDemandMiddleware

    async def ensure_started(**_):  # noqa: ANN003, ANN202
        return False

    monkeypatch.setattr(server_launcher, "ensure_started", ensure_started)

    async def handler(request):  # noqa: ANN001, ANN202
        raise AssertionError("the dispatch must not reach a server that is not there")

    request = SimpleNamespace(tool_call={"name": "start_async_task", "id": "c9"})
    out = asyncio.run(AsyncServerOnDemandMiddleware().awrap_tool_call(request, handler))
    assert out.status == "error" and out.tool_call_id == "c9"
    assert "`task` tool" in out.content


def test_a_planned_server_offers_the_tools_without_running(monkeypatch, tmp_path):
    """Boot must not start a process, yet the async tools have to be offered."""
    from novacode_cli.agents import server_launcher as sl
    from novacode_cli.agents.default_subagents import async_subagents as a
    from novacode_cli.config.config import settings

    project = tmp_path / "other-project"
    project.mkdir()
    monkeypatch.setattr(type(settings), "get_workspace_root", lambda self: project)
    monkeypatch.setattr(a, "_availability", False)  # nothing answers on :2024
    monkeypatch.setattr(sl, "server_extra_available", lambda: True)
    monkeypatch.setitem(sl._state, "process", None)
    monkeypatch.setitem(sl._state, "planned", None)
    monkeypatch.delenv("ASYNC_AGENT_BASE_URL", raising=False)
    monkeypatch.setattr(a, "async_agents_available", a.async_agents_available)  # restore on teardown

    spawned: list = []
    monkeypatch.setattr(sl.AgentServerProcess, "_spawn", lambda self: spawned.append(self))

    try:
        assert sl.plan_agent_server() is True
        assert spawned == [], "planning must not start anything"
        plan = sl._state["planned"]
        assert plan is not None and plan.workspace == project.resolve()
        specs = a.retrieve_async_subagents()
        assert specs and all(s["url"] == plan.url for s in specs)
        assert a.async_agents_see_workspace()
    finally:
        sl.shutdown_agent_server()
    assert not sl.planned()


def test_the_graphs_work_in_the_named_workspace(tmp_path):
    """NOVA_WORKSPACE_ROOT, not the server's own directory, is the graphs' root."""
    (tmp_path / "MARKER.txt").write_text("x", encoding="utf-8")
    code = (
        "from novacode_cli.agents.async_workspace import workspace_root as w;"
        "print(sorted(p.name for p in w().iterdir()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        env={**__import__("os").environ, "NOVA_WORKSPACE_ROOT": str(tmp_path)},
    ).stdout
    assert "MARKER.txt" in out, out


def test_the_background_test_runner_only_runs_test_runners():
    """It runs unattended, with no approvals: it must not be a shell."""
    from novacode_cli.agents.async_agents._test_commands import parse_test_command, run_tests

    assert parse_test_command("pytest tests -x -q") == ["pytest", "tests", "-x", "-q"]
    assert parse_test_command("python -m pytest")[:3] == ["python", "-m", "pytest"]
    assert isinstance(parse_test_command("uv run pytest -k foo"), list)
    assert isinstance(parse_test_command("npm test"), list)

    for bad in (
        "rm -rf .", "pytest; rm -rf .", "pytest && git push", "pytest > out.txt",
        "pytest | tee log", "python -c 'import os'", "git push", "pytest `whoami`",
        "pytest $(whoami)", "npm install", "uv run python evil.py", "",
    ):
        assert isinstance(parse_test_command(bad), str), f"must be refused: {bad!r}"
    assert run_tests.invoke({"command": "git push --force"}).startswith("Refused")


def test_every_registered_graph_has_a_client_spec_and_a_file():
    """A graph the client cannot name, or a spec with no graph, is a dead agent."""
    import json

    from novacode_cli.agents import server_launcher as sl
    from novacode_cli.agents.default_subagents import async_subagents as a

    config = sl._config_path()
    graphs = json.loads(config.read_text(encoding="utf-8"))["graphs"]
    for target in graphs.values():
        assert (config.parent / target.split(":")[0]).is_file(), target
    builders = [getattr(a, n) for n in dir(a) if n.startswith("build_") and n.endswith("_agent")]
    assert {b()["graph_id"] for b in builders} == set(graphs)

# ruff: noqa: ANN001, ANN003, ARG001 — test doubles mirror the signatures
# they replace, so unused/unannotated parameters here are the point, not a smell.
"""The local agent-server launcher: argv, environment, probes and lifecycle.

Nothing here needs ``langgraph-cli``: the command and environment are asserted
directly, the health and readiness probes are pointed at a throwaway HTTP server,
and the lifecycle is driven against a stub child that stands in for the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import pathlib
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from novacode_cli.agents import server_launcher as sl

# ── the command and the environment ────────────────────────────────────────


def test_the_command_is_the_dev_server_this_interpreter_runs():
    command = sl.build_command(
        host="127.0.0.1", port=1234, config_path=pathlib.Path("langgraph.json")
    )

    assert command == [
        sys.executable,
        "-m",
        "langgraph_cli",
        "dev",
        "--host",
        "127.0.0.1",
        "--port",
        "1234",
        "--no-browser",  # a terminal agent must not open a tab
        "--no-reload",  # a reloader would re-exec and orphan the process we track
        "--config",
        str(pathlib.Path("langgraph.json")),
    ]


def test_the_child_env_keeps_the_model_settings_and_drops_loader_hijacks(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    monkeypatch.setenv("NODE_OPTIONS", "--require /evil.js")
    monkeypatch.setenv("PYTHONHOME", "/wrong")
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("DOC_AGENT_MODEL", "gemma4:31b-cloud")

    env = sl.build_env()

    assert "LD_PRELOAD" not in env
    assert "NODE_OPTIONS" not in env
    assert "PYTHONHOME" not in env
    # The graphs read these, so they must survive.
    assert env["OLLAMA_HOST"] == "http://localhost:11434"
    assert env["DOC_AGENT_MODEL"] == "gemma4:31b-cloud"
    # Nova's own policy.
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["LANGGRAPH_AUTH_TYPE"] == "noop"


def test_overrides_win_over_the_inherited_environment(monkeypatch):
    monkeypatch.setenv("DOC_AGENT_MODEL", "inherited")
    assert sl.build_env({"DOC_AGENT_MODEL": "explicit"})["DOC_AGENT_MODEL"] == "explicit"


# ── a throwaway server for the probes ───────────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves whatever ``statuses`` says, then 200s forever."""

    # HTTP/1.1 so connections stay open: the default HTTP/1.0 closes after
    # every response, and httpx then fails reusing the pooled connection.
    protocol_version = "HTTP/1.1"
    # NOT `responses`: http.server keeps its status-text table under that
    # name and send_response() looks the code up in it.
    statuses: ClassVar[list[int]] = [200]

    def do_GET(self) -> None:
        status = self.statuses[0] if self.statuses else 200
        if len(self.statuses) > 1:
            self.statuses.pop(0)
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@contextlib.contextmanager
def _fake_server(*statuses: int) -> Iterator[str]:
    """A real HTTP server on a free port, answering *statuses* in order."""
    handler = type("_H", (_Handler,), {"statuses": list(statuses) or [200]})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_health_waits_until_ok_answers():
    with _fake_server() as url:
        asyncio.run(sl.wait_healthy(url, timeout=5))


def test_health_times_out_and_names_the_last_status():
    with _fake_server(503) as url, pytest.raises(RuntimeError) as caught:
        asyncio.run(sl.wait_healthy(url, timeout=1.5))

    assert "did not become healthy" in str(caught.value)
    assert "503" in str(caught.value)


def test_a_server_that_dies_is_reported_at_once_with_its_log():
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    process.wait(timeout=10)
    try:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(
                sl.wait_healthy(
                    "http://127.0.0.1:1",
                    process=process,
                    read_log=lambda: "import error: no module named 'graph'",
                    timeout=5,
                )
            )
    finally:
        process.wait(timeout=5)

    message = str(caught.value)
    assert "exited during startup" in message
    assert "no module named 'graph'" in message


def test_graph_readiness_waits_for_a_server_that_starts_late():
    """Readiness is retried until the budget runs out, never a one-shot."""
    port = sl.find_free_port("127.0.0.1")
    handler = type("_H", (_Handler,), {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)

    def start_later() -> None:
        time.sleep(0.4)
        server.serve_forever()

    thread = threading.Thread(target=start_later, daemon=True)
    thread.start()
    try:
        asyncio.run(sl.wait_graph_ready(f"http://127.0.0.1:{port}", "g", timeout=6))
    finally:
        server.server_close()


def test_graph_readiness_fails_loudly_after_the_budget():
    with _fake_server(404) as url, pytest.raises(RuntimeError) as caught:
        asyncio.run(sl.wait_graph_ready(url, "missing-agent", timeout=0.5))

    assert "missing-agent" in str(caught.value)


# ── the lifecycle, driven by a stub child ───────────────────────────────────


def _stub_process(**overrides) -> sl.AgentServerProcess:
    return sl.AgentServerProcess(
        config_path=pathlib.Path("langgraph.json"),
        cwd=pathlib.Path(),
        graph_names=["documentation-update-agent"],
        **overrides,
    )


@pytest.fixture
def stubbed_child(monkeypatch):
    """Replace the server with a sleeping child and no-op readiness probes."""
    monkeypatch.setattr(
        sl,
        "build_command",
        lambda **_: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    monkeypatch.setattr(sl, "wait_healthy", _async_noop)
    monkeypatch.setattr(sl, "wait_graph_ready", _async_noop)


async def _async_noop(*args: object, **kwargs: object) -> None:
    return None


def test_start_get_a_running_child_and_stop_really_stops_it(stubbed_child):
    server = _stub_process()

    async def drive() -> dict:
        await server.start()
        state: dict = {"running": server.running, "port": server.port, "url": server.url}
        await server.start()  # already running: must not spawn a second child
        state["same_pid"] = server._process.pid if server._process else None
        await asyncio.to_thread(server.stop)
        state["running_after_stop"] = server.running
        await asyncio.to_thread(server.stop)  # idempotent
        state["stopped_twice"] = server.running
        return state

    out = asyncio.run(drive())

    assert out["running"] is True
    assert out["running_after_stop"] is False
    assert out["stopped_twice"] is False
    assert out["port"] > 0, "an ephemeral port must be resolved before spawning"
    assert out["url"] == f"http://127.0.0.1:{out['port']}"


def test_a_failed_start_leaves_nothing_running(stubbed_child, monkeypatch):
    server = _stub_process()

    async def boom(*args: object, **kwargs: object) -> None:
        msg = "server did not come up"
        raise RuntimeError(msg)

    monkeypatch.setattr(sl, "wait_healthy", boom)

    async def drive() -> bool:
        with pytest.raises(RuntimeError):
            await server.start()
        return server.running

    assert asyncio.run(drive()) is False, "a failed start must not leave a child behind"


def test_a_requested_port_already_in_use_is_replaced(stubbed_child):
    with _fake_server() as url:
        busy = int(url.rsplit(":", 1)[1])
        server = _stub_process(port=busy)
        asyncio.run(server.start())
        try:
            assert server.port != busy
            assert server.running
        finally:
            server.stop()


def test_find_free_port_hands_back_something_bindable():
    port = sl.find_free_port("127.0.0.1")
    assert 0 < port < 65536
    assert sl.port_in_use("127.0.0.1", port) is False


# ── the session policy: when to launch at all ───────────────────────────────


def test_ensure_does_nothing_when_a_server_already_answers(monkeypatch):
    async def reachable(*, refresh: bool = False) -> bool:
        return True

    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_available",
        reachable,
    )
    monkeypatch.setattr(sl, "server_extra_available", lambda: True)
    # An answering server is used only when it is known to work on this project.
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_see_workspace",
        lambda: True,
    )

    assert asyncio.run(sl.ensure_agent_server()) is None


def test_ensure_does_nothing_without_the_extra(monkeypatch):
    async def unreachable(*, refresh: bool = False) -> bool:
        return False

    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.async_agents_available",
        unreachable,
    )
    monkeypatch.setattr(sl, "server_extra_available", lambda: False)

    assert asyncio.run(sl.ensure_agent_server()) is None


def test_ensure_is_a_no_op_when_disabled():
    assert asyncio.run(sl.ensure_agent_server(enabled=False)) is None


def test_the_status_reporter_describes_an_idle_session():
    status = sl.agent_server_status()
    assert status["running"] is False
    assert status["url"] is None
    assert sl.shutdown_agent_server() is None


def test_graphs_come_from_the_packaged_langgraph_json():
    config = sl._config_path()
    assert config.is_file() and "novacode_cli" in config.parts, "it must ship inside the package"
    for target in __import__("json").loads(config.read_text(encoding="utf-8"))["graphs"].values():
        assert (config.parent / target.split(":")[0]).is_file(), target
    graphs = sl._graph_names(config)

    assert "documentation-update-agent" in graphs
    assert "plan-scout-agent" in graphs
    assert len(graphs) == 9
    assert sl._graph_names(config.parent / "nope.json") == []


def test_the_health_probe_is_fast_enough_to_build_an_env():
    """The port probe sits on the agent-build path, so it has to be quick."""
    started = time.monotonic()
    sl.port_in_use("127.0.0.1", 1)
    assert time.monotonic() - started < 1.0

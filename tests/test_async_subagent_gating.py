"""Async subagents are only offered while their server is actually running.

They execute on the ``novacode`` LangGraph container. Advertising them when it
is stopped turns every delegation into a failed round-trip, so the specs are
withheld and the agent uses its ordinary in-process subagents instead.
"""

from __future__ import annotations

import socket

import pytest

from novacode_cli.agents.default_subagents import async_subagents as mod


@pytest.fixture(autouse=True)
def _clear_probe_cache(monkeypatch):
    """The reachability probe is cached per process; each test starts fresh."""
    monkeypatch.setattr(mod, "_availability", None, raising=False)


def _closed_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed again on exit


def test_no_async_specs_when_the_server_is_down(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    assert mod.async_agents_available() is False
    assert mod.retrieve_async_subagents() == [], "fall back to synchronous subagents"


def test_all_specs_when_the_server_answers(monkeypatch):
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{srv.getsockname()[1]}")
        assert mod.async_agents_available() is True
        specs = mod.retrieve_async_subagents()
    assert len(specs) == 6
    assert all(spec["url"] for spec in specs), "a spec without a URL cannot be reached"


def test_the_url_defaults_instead_of_falling_back_to_in_process_asgi(monkeypatch):
    """`None` meant an ASGI transport that only exists inside `langgraph dev`."""
    monkeypatch.delenv("ASYNC_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("LANGGRAPH_API_URL", raising=False)
    assert mod._resolve_async_agent_url(2024) == "http://localhost:2024"


def test_a_base_url_that_already_names_a_port_is_not_double_suffixed(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_BASE_URL", "http://localhost:2024")
    assert mod._resolve_async_agent_url(2024) == "http://localhost:2024"


def test_an_unusable_base_url_reports_unavailable_rather_than_probing_localhost(monkeypatch):
    monkeypatch.setenv("ASYNC_AGENT_BASE_URL", "not a url at all")
    assert mod.async_agents_available() is False
    assert mod.retrieve_async_subagents() == []


def test_the_probe_is_cached(monkeypatch):
    calls = []
    real = socket.create_connection

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(socket, "create_connection", counting)
    for _ in range(3):
        mod.async_agents_available()
    assert len(calls) == 1, "one probe per process, not one per agent build"


# ── the prompt follows the same gate ─────────────────────────────────────────


def _subagents_block(*, available: bool) -> str:
    from novacode_cli.prompts import render_template

    rendered = render_template(
        "System_Prompt_Nova.jinja", has_tavily=False, has_async_agents=available
    )
    return rendered.split("<subagents>")[1].split("</subagents>")[0]


def test_the_prompt_omits_async_tools_when_the_server_is_down():
    """Describing unbound tools wastes context and invites a call that fails."""
    block = _subagents_block(available=False)
    for tool in ("start_async_task", "check_async_task", "cancel_async_task"):
        assert tool not in block, tool
    assert "`task`" in block, "the sync subagent is always there"


def test_the_prompt_encourages_delegation_when_the_server_is_up():
    block = _subagents_block(available=True)
    assert "start_async_task" in block
    assert "Default to async" in block, "the old wording only said when NOT to"
    assert len(block) > len(_subagents_block(available=False))


def test_the_rendered_instructions_track_availability(monkeypatch):
    """get_default_coding_instructions probes rather than assuming."""
    from novacode_cli.config.config import get_default_coding_instructions

    monkeypatch.setenv("ASYNC_AGENT_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    assert "start_async_task" not in get_default_coding_instructions()

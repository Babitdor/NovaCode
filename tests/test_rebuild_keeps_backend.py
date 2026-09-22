"""A model switch / MCP reload must rebuild the agent on the SAME backend.

The rebuild passed ``sandbox=self._backend if hasattr(self._backend, "default")``
— but ``_backend`` is the CompositeBackend, which always has ``.default``. So in
local mode, after the first /model switch, core_agent wrapped the local
filesystem as a ``/workspace`` sandbox: read_file/edit_file failed with
"unexpected server response: <no output>" and write_file reported success while
the file landed in a stray ``<project>/workspace/`` folder. Found in the saved
sessions of three projects.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import novacode_cli.agents.core_agent as core_agent
from novacode_cli.states.slices.agent_runtime import AgentRuntimeState


@pytest.fixture()
def builds(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def _fake_build(**kw):  # noqa: ANN003, ANN202
        seen.append(kw)
        return object(), SimpleNamespace(default=object())

    monkeypatch.setattr(core_agent, "create_agent_with_config", _fake_build)
    monkeypatch.setattr("novacode_cli.mcp.reset_shared_mcp_middleware", lambda: None)
    return seen


def _runtime(sandbox_type: str | None, sandbox: object | None) -> AgentRuntimeState:
    rt = AgentRuntimeState()
    rt.set_agent_context(
        agent=object(),
        backend=SimpleNamespace(default=object()),  # the composite
        checkpointer=object(),
        store=object(),
        tools=[],
        assistant_id="a",
        model=object(),
        sandbox_type=sandbox_type,
        sandbox=sandbox,
    )
    return rt


@pytest.mark.parametrize("rebuild", ["switch_model", "reload_mcp_servers"])
def test_local_mode_stays_local_after_a_rebuild(builds, rebuild) -> None:
    rt = _runtime("none", sandbox=None)
    if rebuild == "switch_model":
        asyncio.run(rt.switch_model(object()))
    else:
        asyncio.run(rt.reload_mcp_servers())
    assert builds[-1]["sandbox"] is None, "the composite was passed off as a sandbox"
    # A second rebuild must not pick up the NEW composite either.
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["sandbox"] is None


def test_a_real_sandbox_is_reused_not_the_composite(builds) -> None:
    docker = object()
    rt = _runtime("docker", sandbox=docker)
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["sandbox"] is docker
    assert builds[-1]["exec_sandbox"] is False


def test_os_confinement_survives_a_rebuild(builds) -> None:
    rt = _runtime("os", sandbox=None)
    asyncio.run(rt.switch_model(object()))
    assert builds[-1]["exec_sandbox"] is True, "the shell lost its OS sandbox"

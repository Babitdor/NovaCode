"""Canonical execution discovery and legacy calls share existing guards and engines."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import Mock

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.backends.filesystem import OptimizedLocalShellBackend
from novacode_cli.security.delegated_approval import HardDenyMiddleware
from novacode_cli.security.policy import get_policy
from novacode_cli.shell.middleware import ShellMiddleware
from novacode_cli.shell.tool_surface import execution_tools
from tests.test_mcp_code_mode import ScriptedModel, isolated_policy  # noqa: F401

pytestmark = pytest.mark.timeout(35)


@pytest.fixture
def execution(tmp_path):
    backend = OptimizedLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    fs = FilesystemMiddleware(backend=backend)
    shell = ShellMiddleware(workspace_root=str(tmp_path), backend=backend)
    get_policy().tool_tiers.update({"execute": "allow", "shell": "allow", "bash": "allow"})
    return fs, shell


async def run(execution, name, arguments, code=False):
    fs, shell = execution
    calls = [{"id": "execution", "name": name, "args": arguments}]
    graph = create_agent(
        ScriptedModel(
            messages=iter(
                [
                    AIMessage(content="", tool_calls=calls),
                    AIMessage(content="done"),
                ]
            )
        ),
        middleware=[*([CodeModeMiddleware()] if code else []), fs, shell, HardDenyMiddleware()],
    )
    result = await graph.ainvoke({"messages": [HumanMessage("Run the isolated command")]})
    return [m for m in result["messages"] if isinstance(m, ToolMessage)][-1]


def test_single_advertised_executor_preserves_registry_and_discovery(execution):
    fs, shell = execution
    registered = [*fs.tools, *shell.tools]
    original = next(t for t in registered if t.name == "execute")
    convert_to_openai_tool(original)  # A warmed schema cache must not hide the new selector.
    public = execution_tools(registered)
    assert {t.name for t in public}.isdisjoint({"shell", "bash"})
    target = next(t for t in public if t.name == "execute")
    schema = convert_to_openai_tool(target)["function"]["parameters"]
    assert "shell" in schema["required"]
    assert "runtime" not in schema["properties"]
    assert schema["properties"]["shell"]["enum"] == ["native", "bash"]
    assert "shell" not in convert_to_openai_tool(original)["function"]["parameters"]["properties"]
    assert {t.name for t in registered} >= {"shell", "bash", "execute"}
    assert {hit["name"] for hit in ToolSearchMiddleware()._catalog_for(registered)}.isdisjoint(
        {"shell", "bash"}
    )


def test_approval_keeps_shell_selection_visible_and_arg_dependent(execution):
    from types import SimpleNamespace

    from novacode_cli.hitl.interrupts import get_interrupt_configs
    from novacode_cli.ui.hitl_approval import evaluate_tool_actions

    policy = get_policy()
    policy.tool_tiers["shell"] = "ask"
    policy.shell_allow.clear()
    policy._shell_allow_re.clear()
    config = get_interrupt_configs()["execute"]
    args = {"command": "fixture_action", "shell": "native", "background": True}
    description = config["description"]({"name": "execute", "args": args}, None, None)
    assert "Shell: native" in description
    assert "Background: True" in description
    assert "runtime" not in config["args_schema"]["properties"]
    assert config["args_schema"]["properties"]["shell"]["enum"] == ["native", "bash"]
    assert evaluate_tool_actions(
        {"action_requests": [{"name": "execute", "args": args}]},
        SimpleNamespace(auto_approve=False),
        policy=policy,
    ) == [None]


@pytest.mark.parametrize(
    "arguments", [{"command": "echo legacy-ok"}, {"command": "echo native-ok", "shell": "native"}]
)
async def test_actual_graph_accepts_legacy_and_explicit_calls(execution, arguments):
    result = await run(execution, "execute", arguments)
    assert result.status == "success", result.content
    assert "-ok" in result.content


async def test_code_mode_describes_new_schema_but_retains_old_calls(execution):
    result = await run(
        execution,
        "MCPCode",
        {
            "code": "const schema = await tools.describe('execute'); "
            "const names = (await tools.list({limit:50})).tools.map(t=>t.name); "
            "const old = await tools.call('execute',{command:'echo legacy-ok'}); "
            "const current = await tools.call('execute',{command:'echo native-ok',shell:'native'}); "
            "return {required:schema.schema.parameters.required,names,old,current};"
        },
        code=True,
    )
    assert result.status == "success", result.content
    value = json.loads(result.content)["result"]
    assert "shell" in value["required"]
    assert "shell" not in value["names"] and "bash" not in value["names"]
    assert "legacy-ok" in value["old"] and "native-ok" in value["current"]


@pytest.mark.parametrize("selector,alias", [("native", "shell"), ("bash", "bash")])
@pytest.mark.parametrize("tier", ["ask", "deny"])
def test_shell_selection_keeps_the_more_restrictive_policy(execution, selector, alias, tier):
    policy = get_policy()
    policy.tool_tiers[alias] = tier
    policy.shell_allow.clear()
    policy._shell_allow_re.clear()
    assert policy.evaluate("execute", {"command": "fixture_action", "shell": selector}).tier == tier
    assert policy.evaluate("execute", {"command": "fixture_action"}).tier == "allow"


async def test_denied_bash_cannot_run_through_execute(execution, monkeypatch):
    get_policy().tool_tiers["bash"] = "deny"
    reached = Mock(side_effect=AssertionError("Denied command reached execution"))
    monkeypatch.setattr(ShellMiddleware, "_adispatch_local", reached)
    result = await run(execution, "execute", {"command": "echo must-not-run", "shell": "bash"})
    assert result.status == "error"
    reached.assert_not_called()


@pytest.mark.parametrize("shell_name", ["native", "bash"])
async def test_explicit_dialects_run_once_and_keep_per_call_state(
    execution, monkeypatch, shell_name
):
    _, shell = execution
    shell._native_prog = ["powershell", "-Command"]
    shell._bash_prog = ["bash", "-c"]
    original_timeout = shell._timeout
    observed = []

    async def foreground(engine, command, *, tool_call_id, prog):
        observed.append((engine._timeout, engine._tool_name, prog))
        return ToolMessage(
            content="syntax error: rejected", tool_call_id=tool_call_id, status="error"
        )

    monkeypatch.setattr(ShellMiddleware, "_arun_foreground", foreground)
    result = await run(
        execution, "execute", {"command": "fixture_action", "shell": shell_name, "timeout": 3}
    )
    assert result.status == "error"
    assert observed == [
        (3, "execute", shell._bash_prog if shell_name == "bash" else shell._native_prog)
    ]
    assert shell._timeout == original_timeout and shell._tool_name == "shell"


@pytest.mark.parametrize("selector", ["bash", "unavailable"])
async def test_invalid_or_missing_shell_fails_without_dispatch(execution, monkeypatch, selector):
    _, shell = execution
    shell._bash_prog = None
    reached = Mock(side_effect=AssertionError("Invalid shell reached execution"))
    monkeypatch.setattr(ShellMiddleware, "_adispatch_local", reached)
    result = await run(execution, "execute", {"command": "echo blocked", "shell": selector})
    assert result.status == "error"
    reached.assert_not_called()


async def test_timeout_uses_existing_process_termination(execution):
    _, shell = execution
    shell._native_prog = [sys.executable, "-c"]
    original_timeout = shell._timeout
    result = await run(
        execution,
        "execute",
        {"command": "import time; time.sleep(8)", "shell": "native", "timeout": 1},
    )
    assert result.status == "error", result.content
    assert "exceeded" in result.content or "timed out" in result.content
    assert shell._timeout == original_timeout


async def test_cancellation_propagates_without_replay(execution, monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def dispatch(engine, command, **kwargs):
        calls.append(command)
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(ShellMiddleware, "_adispatch_local", dispatch)
    task = asyncio.create_task(
        run(execution, "execute", {"command": "fixture_action", "shell": "native"})
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert calls == ["fixture_action"]


async def test_parallel_calls_keep_independent_deadlines(execution, monkeypatch):
    fs, shell = execution
    ready = asyncio.Event()
    observed = []

    async def foreground(engine, command, *, tool_call_id, prog):
        observed.append((command, engine._timeout))
        if len(observed) == 2:
            ready.set()
        await ready.wait()
        assert engine._timeout == (1 if command == "first" else 3)
        return ToolMessage(content=command, tool_call_id=tool_call_id)

    monkeypatch.setattr(ShellMiddleware, "_arun_foreground", foreground)
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": name,
                            "name": "execute",
                            "args": {"command": name, "shell": "native", "timeout": timeout},
                        }
                        for name, timeout in [("first", 1), ("second", 3)]
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    graph = create_agent(model, middleware=[fs, shell, HardDenyMiddleware()])
    await graph.ainvoke({"messages": [HumanMessage("Run independent fixture commands")]})
    assert sorted(observed) == [("first", 1), ("second", 3)]
    assert shell._timeout == 120


async def test_background_and_interactive_flags_reach_the_existing_engine(execution, monkeypatch):
    calls = []

    async def dispatch(engine, command, **kwargs):
        calls.append((engine._tool_name, kwargs["background"], kwargs["interactive"]))
        return ToolMessage(content="started", tool_call_id=kwargs["tool_call_id"])

    monkeypatch.setattr(ShellMiddleware, "_adispatch_local", dispatch)
    result = await run(
        execution,
        "execute",
        {"command": "fixture_action", "shell": "native", "background": True, "interactive": True},
    )
    assert result.status == "success"
    assert calls == [("execute", True, True)]


@pytest.mark.parametrize("selector", [None, "native", "bash"])
async def test_sandbox_execution_stays_on_backend_and_preserves_timeout(
    tmp_path, monkeypatch, selector
):
    from tests.test_workdir_backend import _FakeSandbox

    backend = _FakeSandbox()
    requests = []

    async def capture(command, *, timeout=None):
        requests.append((command, timeout))
        return backend.execute(command, timeout=timeout)

    monkeypatch.setattr(backend, "aexecute", capture)
    fs = FilesystemMiddleware(backend=backend)
    shell = ShellMiddleware(workspace_root=str(tmp_path), backend=backend)
    get_policy().tool_tiers.update({"execute": "allow", "shell": "allow", "bash": "allow"})
    reached = Mock(side_effect=AssertionError("Sandbox command reached host shell"))
    monkeypatch.setattr(ShellMiddleware, "_adispatch_local", reached)
    args = {"command": "printf 'sandbox value'", "timeout": 7}
    if selector is not None:
        args["shell"] = selector
    result = await run((fs, shell), "execute", args)
    assert result.status == "success", result.content
    assert len(requests) == 1
    assert requests[0][1] == 7
    if selector == "bash":
        assert requests[0][0].startswith("bash -c ")
    else:
        assert requests[0][0] == args["command"]
    reached.assert_not_called()

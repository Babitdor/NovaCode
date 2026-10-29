"""Composed authorization and project/session isolation regressions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.tools import tool

from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware
from novacode_cli.config.plan_mode import plan_tool_rejection
from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware
from novacode_cli.events import nova_event_log
from novacode_cli.security import policy as policy_module
from novacode_cli.security.delegated_approval import HardDenyMiddleware
from novacode_cli.security.policy import load_policy
from novacode_cli.security.rule_synthesis import synthesize_rule
from novacode_cli.security.session_allow import get_session_allow, reset_session_allow
from novacode_cli.session.runtime_identity import RuntimeIdentity, current_identity
from novacode_cli.shell.middleware import ShellMiddleware

if TYPE_CHECKING:
    from pathlib import Path


@tool
def execute(command: str) -> str:  # noqa: ARG001 -- backend must never execute
    """A backend execution boundary that should be replaced by the shell."""
    message = "Original execute must not run"
    raise AssertionError(message)


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(policy_module, "_read_json", lambda _: {})
    result = load_policy(tmp_path)
    monkeypatch.setattr(policy_module, "_CACHED_POLICY", result)
    return result


@pytest.mark.parametrize("guard", ["plan", "workspace", "deny", "allow"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_local_execute_traverses_guards(
    tmp_path: Path,
    policy: policy_module.ApprovalPolicy,
    guard: str,
    *,
    asynchronous: bool,
):
    shell = ShellMiddleware.__new__(ShellMiddleware)
    shell._supports_sandbox_execution = lambda: False
    shell._dispatch_local = Mock(return_value="local result")
    shell._adispatch_local = AsyncMock(return_value="local result")
    middleware = {
        "plan": PlanModeMiddleware(tmp_path),
        "workspace": CoworkBrokerMiddleware(tmp_path),
        "deny": HardDenyMiddleware(),
        "allow": PlanModeMiddleware(tmp_path, always_on=False),
    }[guard]
    if guard == "deny":
        policy.tool_tiers["execute"] = "deny"
    request = ToolCallRequest(
        tool_call={"name": "execute", "id": "exec", "args": {"command": "echo probe"}},
        tool=execute,
        state={},
        runtime=None,
    )

    def terminal(req: ToolCallRequest) -> object:
        return req.tool.invoke(req.tool_call["args"])

    async def aterminal(req: ToolCallRequest) -> object:
        return await req.tool.ainvoke(req.tool_call["args"])

    if asynchronous:
        result = await shell.awrap_tool_call(
            request, lambda req: middleware.awrap_tool_call(req, aterminal)
        )
    else:
        result = shell.wrap_tool_call(request, lambda req: middleware.wrap_tool_call(req, terminal))
    if guard == "allow":
        assert result == "local result"
    else:
        assert result.status == "error"
        shell._dispatch_local.assert_not_called()
        shell._adispatch_local.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "python_kernel",
        "daemon",
        "eval",
        "skill_manage",
        "wiki_write",
        "wiki_update_index",
        "remember",
        "forget",
        "ui_patch",
        "ui_commit",
        "ui_rollback",
        "sleep_desktop",
        "restore_file",
        "restart_task",
        "terminate_task",
        "create_artifact",
        "update_artifact",
    ],
)
def test_plan_rejects_native_mutations(name: str):
    assert plan_tool_rejection(name, {}) is not None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_plan_rejects_mutating_http_methods(method: str):
    assert plan_tool_rejection("fetch_url", {"method": method}) is not None


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_plan_keeps_http_investigation(method: str):
    assert plan_tool_rejection("fetch_url", {"method": method}) is None


@pytest.mark.parametrize(
    "command",
    [
        "git status-owned",
        "git logevil",
        "git diff-custom",
        "git showanything",
        "git remote -v add review https://example.invalid/repo.git",
        "git remote -v remove origin",
        "git remote -v set-url origin https://example.invalid/x",
        "git status --help",
        "git log -o result",
        "git rev-parse --output-object-format=sha1",
    ],
)
def test_git_noninspection_requires_approval(policy: policy_module.ApprovalPolicy, command: str):
    assert policy.evaluate("shell", {"command": command}).tier != "allow"


@given(
    command=st.sampled_from(
        ["status", "diff", "log", "show", "rev-parse", "describe", "blame", "shortlog"]
    ),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=24),
)
def test_git_alias_suffixes_are_never_builtin_inspection(command: str, suffix: str):
    policy = policy_module.ApprovalPolicy(
        dict(policy_module._DEFAULT_TOOL_TIERS),
        list(policy_module._DEFAULT_SHELL_ALLOW),
        list(policy_module._DEFAULT_SHELL_DENY),
        [],
        [],
        [],
        [],
    )
    assert policy.evaluate("shell", {"command": f"git {command}{suffix}"}).tier != "allow"


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --stat",
        "git log -3 --oneline",
        "git show HEAD",
        "git remote -v",
    ],
)
def test_git_inspection_still_allowed(policy: policy_module.ApprovalPolicy, command: str):
    assert policy.evaluate("shell", {"command": command}).tier == "allow"


def test_approval_callback_targets_originating_session(policy: policy_module.ApprovalPolicy):
    from types import SimpleNamespace

    from novacode_cli.security.remember import apply_remember
    from novacode_cli.ui.hitl_approval import evaluate_tool_actions

    reset_session_allow()
    args = {"command": "npm run build"}
    apply_remember("session", "shell", args, session="origin")
    request = {"action_requests": [{"name": "shell", "args": args}], "approval_session": "origin"}
    state = SimpleNamespace(auto_approve=False)
    assert evaluate_tool_actions(request, state, policy=policy) == [{"type": "approve"}]
    request["approval_session"] = "other"
    assert evaluate_tool_actions(request, state, policy=policy) == [None]
    reset_session_allow()


async def test_session_close_releases_owned_state(tmp_path: Path):
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.session.checkpoint_coordinator import CheckpointCoordinator, CoordinatedGraph
    from novacode_cli.tools.python_kernel_tool import _get_kernel, _kernels

    identity = RuntimeIdentity.from_config(str(tmp_path), {"configurable": {"thread_id": "t"}}, "")
    token = current_identity.set(identity)
    try:
        kernel = await _get_kernel()
        await kernel.request({"code": "print(1)"}, 5)
        proc = kernel._proc
        get_session_allow().add(synthesize_rule("shell", {"command": "npm run build"}))
        nova_event_log.append(("test", "", "", "owned"))
        graph = CoordinatedGraph(object(), CheckpointCoordinator(InMemorySaver()), str(tmp_path))
        await graph.close_session("t")
        assert proc.returncode is not None
        assert identity.session not in _kernels
        assert list(nova_event_log) == []
        assert not get_session_allow().matches("shell", {"command": "npm run build"})
    finally:
        current_identity.reset(token)


async def test_events_are_isolated_and_inherited_by_worker_threads():
    first = RuntimeIdentity.from_config("one", {"configurable": {"thread_id": "t"}}, "")
    second = RuntimeIdentity.from_config("two", {"configurable": {"thread_id": "t"}}, "")
    token = current_identity.set(first)
    try:
        nova_event_log.clear()
        await asyncio.to_thread(nova_event_log.append, ("test", "", "", "first"))
        other = current_identity.set(second)
        try:
            assert list(nova_event_log) == []
            nova_event_log.append(("test", "", "", "second"))
        finally:
            current_identity.reset(other)
        assert list(nova_event_log) == [("test", "", "", "first")]
        nova_event_log.clear()
    finally:
        current_identity.reset(token)


def test_remembered_approval_is_scoped_to_runtime_identity():
    reset_session_allow()
    token = current_identity.set(
        RuntimeIdentity.from_config("one", {"configurable": {"thread_id": "t"}}, "")
    )
    try:
        first = get_session_allow()
        first.add(synthesize_rule("shell", {"command": "npm run build"}))
        other = current_identity.set(
            RuntimeIdentity.from_config("two", {"configurable": {"thread_id": "t"}}, "")
        )
        try:
            assert not get_session_allow().matches("shell", {"command": "npm run build"})
        finally:
            current_identity.reset(other)
        assert get_session_allow() is first
    finally:
        current_identity.reset(token)
        reset_session_allow()


async def test_real_graph_local_execute_cannot_skip_workspace_broker(tmp_path: Path):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage

    from tests.test_mcp_code_mode import ScriptedModel

    shell = ShellMiddleware(workspace_root=str(tmp_path))
    shell._adispatch_local = AsyncMock(return_value="must not execute")
    model = ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "id": "exec",
                            "args": {"command": "echo probe"},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    graph = create_agent(
        model, tools=[execute], middleware=[shell, CoworkBrokerMiddleware(tmp_path)]
    )
    state = await graph.ainvoke({"messages": [{"role": "user", "content": "inspect"}]})
    results = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert results[0].status == "error"
    assert "Cowork broker" in results[0].content
    shell._adispatch_local.assert_not_called()

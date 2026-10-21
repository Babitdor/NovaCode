"""Unattended subagents approve ordinary tools even with a manual parent."""

from types import SimpleNamespace

import pytest

from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
from novacode_cli.security.policy import ApprovalPolicy


@pytest.fixture
def policy(monkeypatch):
    policy = ApprovalPolicy(
        tool_tiers={"forbidden": "deny"},
        shell_allow=[],
        shell_deny=["blocked-command"],
        path_allow=[],
        path_deny=["**/secret.txt"],
        domain_allow=[],
        domain_deny=["evil.example"],
    )
    monkeypatch.setattr("novacode_cli.security.delegated_approval.get_policy", lambda: policy)
    return policy


@pytest.mark.parametrize(
    "name,args",
    [
        ("skills_load", {"name": "web-research"}),
        ("execute", {"command": "echo delegated"}),
        ("write_file", {"file_path": "/notes.md", "content": "research"}),
        ("NotebookEditTool", {"file_path": "/analysis.ipynb"}),
        ("fetch_url", {"url": "https://docs.python.org"}),
        ("read_file", {"file_path": "/notes.md"}),
    ],
)
@pytest.mark.parametrize(
    "context", [None, {"auto_approve": False}, SimpleNamespace(auto_approve=False)]
)
async def test_sync_and_async_delegates_auto_approve(policy, name, args, context):
    from novacode_cli.tools.plan_mode_tools import _auto_approve_var

    token = _auto_approve_var.set(False)
    try:
        request = SimpleNamespace(
            tool_call={"name": name, "args": args, "id": "call-1"},
            runtime=SimpleNamespace(context=context),
        )
        mw = DelegatedApprovalMiddleware()
        assert mw.wrap_tool_call(request, lambda _: "ran") == "ran"

        async def run(_):
            return "ran"

        assert await mw.awrap_tool_call(request, run) == "ran"
        assert _auto_approve_var.get() is False
    finally:
        _auto_approve_var.reset(token)


@pytest.mark.parametrize(
    "name,args",
    [
        ("forbidden", {}),
        ("skills_load", {}),
        ("execute", {"command": "blocked-command"}),
        ("write_file", {"file_path": "/secret.txt"}),
        ("fetch_url", {"url": "https://evil.example/x"}),
    ],
)
async def test_explicit_denies_never_execute(policy, name, args):
    if name == "skills_load":
        policy.tool_tiers[name] = "deny"
    request = SimpleNamespace(tool_call={"name": name, "args": args, "id": "denied"})
    mw = DelegatedApprovalMiddleware()

    def run(_):
        pytest.fail("Denied delegate tool reached its handler")

    async def arun(_):
        pytest.fail("Denied delegate tool reached its async handler")

    for result in (mw.wrap_tool_call(request, run), await mw.awrap_tool_call(request, arun)):
        assert result.status == "error" and result.tool_call_id == "denied"
        assert "denied by approval policy" in result.content
        assert "requires parent authorization" not in result.content


async def test_main_agent_hard_denies_are_unchanged(policy):
    from novacode_cli.security.delegated_approval import HardDenyMiddleware

    mw = HardDenyMiddleware()
    request = SimpleNamespace(tool_call={"name": "skills_load", "args": {}, "id": "main"})
    assert mw.wrap_tool_call(request, lambda _: "ran") == "ran"

    async def run(_):
        return "ran"

    assert await mw.awrap_tool_call(request, run) == "ran"
    request.tool_call["name"] = "forbidden"
    assert mw.wrap_tool_call(request, lambda _: pytest.fail("deny bypass")).status == "error"
    assert (await mw.awrap_tool_call(request, run)).status == "error"


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_nested_research_agent_loads_skill_with_manual_parent(policy, asynchronous):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import tool

    from novacode_cli.agents.core_agent import _harden_subagent_specs
    from novacode_cli.tools.plan_mode_tools import _auto_approve_var

    class Model(GenericFakeChatModel):
        def bind_tools(self, *args, **kwargs):
            return self

    loaded = []

    @tool
    def skills_load(name: str) -> str:
        """Load a research skill."""
        loaded.append(name)
        return "skill ready"

    child = Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "skills_load", "args": {"name": "web-research"}, "id": "skill"}
                    ],
                ),
                AIMessage(content="skill ready"),
            ]
        )
    )
    parent = Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "Load the research skill",
                                "subagent_type": "research-agent",
                            },
                            "id": "delegate",
                        }
                    ],
                ),
                AIMessage(content="research complete"),
            ]
        )
    )
    specs = _harden_subagent_specs(
        [
            {
                "name": "research-agent",
                "description": "Research with a skill",
                "system_prompt": "Load the research skill.",
                "model": child,
                "tools": [skills_load],
            }
        ]
    )
    if not asynchronous:
        # Nova's full hardening stack is async-only (URL screening, etc.).
        # Exercise the shared approval guard in a genuinely synchronous graph.
        specs[0]["middleware"] = [DelegatedApprovalMiddleware()]
    graph = create_deep_agent(model=parent, subagents=specs)
    token = _auto_approve_var.set(False)
    try:
        inputs = {"messages": [HumanMessage(content="Research this project")]}
        result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)
        assert loaded == ["web-research"]
        assert any(
            isinstance(m, ToolMessage) and "skill ready" in m.content for m in result["messages"]
        )
        assert _auto_approve_var.get() is False
    finally:
        _auto_approve_var.reset(token)

"""Enforce policy for unattended agents that cannot present HITL prompts."""

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from novacode_cli.security.policy import get_policy


def _parent_auto_approves(request) -> bool:
    """Whether the session that dispatched this agent is in auto-approve.

    In process (sync and dynamic subagents) that is the turn's own flag, which the
    agent loop sets; on the agent server (async agents) it is the run's context,
    stamped when the run was launched. Either way the subagent gets the answer the
    main agent would have got, instead of a refusal it cannot appeal.
    """
    from novacode_cli.tools.plan_mode_tools import _auto_approve_var

    if _auto_approve_var.get():
        return True
    context = getattr(getattr(request, "runtime", None), "context", None)
    if isinstance(context, dict):
        return bool(context.get("auto_approve"))
    return bool(getattr(context, "auto_approve", False))


class DelegatedApprovalMiddleware(AgentMiddleware):
    """Refuse actions requiring approval; the parent must perform them instead.

    Unless the parent session is in auto-approve: then only a deny is refused.
    """

    def _denial(self, request):
        call = request.tool_call
        name = call.get("name", "")
        policy = get_policy()
        verdict = policy.evaluate(name, call.get("args") or {})
        readonly = {"read_file", "ls", "glob", "grep", "read_agent_messages", "send_agent_message"}
        if verdict.tier == "allow" or (
            name in readonly and policy.tool_default(name) != "deny" and verdict.tier != "deny"
        ):
            return None
        if verdict.tier != "deny" and _parent_auto_approves(request):
            return None
        return ToolMessage(
            content=f"Delegated action {name} requires parent authorization; ask the parent to perform it.",
            tool_call_id=call.get("id", ""),
            name=name,
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        denial = self._denial(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denial = self._denial(request)
        return denial if denial is not None else await handler(request)


class HardDenyMiddleware(DelegatedApprovalMiddleware):
    """Enforce explicit denies even for tools without a HITL interrupt."""

    def _denial(self, request):
        call = request.tool_call
        if get_policy().evaluate(call.get("name", ""), call.get("args") or {}).tier != "deny":
            return None
        return ToolMessage(
            content="Tool action denied by approval policy.",
            tool_call_id=call.get("id", ""),
            name=call.get("name", ""),
            status="error",
        )

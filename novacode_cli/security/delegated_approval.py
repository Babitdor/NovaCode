"""Enforce policy for unattended agents that cannot present HITL prompts."""

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from novacode_cli.security.policy import get_policy


class DelegatedApprovalMiddleware(AgentMiddleware):
    """Refuse actions requiring approval; the parent must perform them instead."""

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

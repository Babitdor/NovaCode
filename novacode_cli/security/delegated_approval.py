"""Auto-approve unattended agents while enforcing explicit policy denies."""

import asyncio
from types import SimpleNamespace

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from novacode_cli.security.policy import get_policy


class DelegatedApprovalMiddleware(AgentMiddleware):
    """Review unattended actions without opening a child approval dialog.

    Subagents cannot present an approval UI. Explicit denies still apply, as do
    separate plan-mode, workspace broker, and shell confinement middleware.
    This does not change the main session's approval setting.
    """

    def _denial(self, request):
        call = request.tool_call
        name = call.get("name", "")
        policy = get_policy()
        from novacode_cli.security.approval_context import request_user_text
        from novacode_cli.ui.hitl_approval import evaluate_tool_actions

        decisions = evaluate_tool_actions(
            {"action_requests": [{"name": name, "args": call.get("args") or {}}]},
            SimpleNamespace(auto_approve=True),
            user_request=request_user_text(request, delegated=True),
            policy=policy,
        )
        decision = decisions[0]
        if decision is not None and decision.get("type") == "approve":
            return None
        reason = (
            decision.get("reason", "approval rejected") if decision is not None
            else "Owner approval required; return this action to the parent without executing it."
        )
        return ToolMessage(
            content=f"Delegated action {name} denied by approval policy: {reason}",
            tool_call_id=call.get("id", ""),
            name=name,
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        denial = self._denial(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denial = await asyncio.to_thread(self._denial, request)
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

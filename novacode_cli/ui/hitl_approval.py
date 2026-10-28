"""Human-in-the-loop approval flow for tool actions.

This module provides the shared policy evaluation functions used by the
agent loop, headless runner, and TUI. The REPL-specific approval prompts
have been removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novacode_cli.security.policy import ApprovalPolicy

from novacode_cli.config.config import console
from novacode_cli.config.plan_mode import plan_tool_rejection
from novacode_cli.security.session_allow import get_session_allow


def _confirm_remember(rule):  # noqa: ANN001, ANN202
    """Confirm an 'always allow' rule: show it, pick project/global, optional edit.

    Returns ``(target, edited_rule_or_None)`` where target is ``"project"`` /
    ``"global"``, or ``(None, None)`` if the user cancels.
    """
    from dataclasses import replace

    console.print()
    console.print(f"[bold]Always allow:[/bold] {rule.human}")
    console.print(f"[dim]Rule ({rule.category}):[/dim] {rule.value}")
    choice = input("Save where?  [P]roject  [G]lobal  [E]dit  [C]ancel: ").strip().lower()
    if choice in {"e", "edit"}:
        new_value = input(f"Edit rule value [{rule.value}]: ").strip() or rule.value
        rule = replace(rule, value=new_value)
        console.print(f"[dim]Updated rule: {rule.value}[/dim]")
        choice = input("Save where?  [P]roject  [G]lobal  [C]ancel: ").strip().lower()
    if choice in {"p", "project"}:
        return "project", rule
    if choice in {"g", "global"}:
        return "global", rule
    return None, None


def check_plan_mode_blocked(req: dict, plan_mode_enabled: bool) -> tuple[bool, dict | None]:
    """Check if a tool action is blocked by plan mode.

    Args:
        req: The tool action request dict with "action_requests" list
        plan_mode_enabled: Whether plan mode is active

    Returns:
        Tuple of (blocked, rejection_dict_or_None)
    """
    if not plan_mode_enabled:
        return False, None

    action_requests = req.get("action_requests", [])
    decisions = []
    any_rejected = False

    for ar in action_requests:
        tool_name = ar.get("name", "")
        reason = plan_tool_rejection(tool_name, ar.get("args") or {})
        if reason:
            decisions.append({
                "type": "reject", "reason": reason,
                "message": (
                    "writes/edits are only allowed inside .nova/plans/ or .nova/ralph/ in plan mode"
                )
                if tool_name in {"write_file", "edit_file"} else reason,
            })
            any_rejected = True
        else:
            decisions.append({"type": "approve"})

    if any_rejected:
        return True, {"decisions": decisions, "any_rejected": True}
    return False, None


def evaluate_tool_actions(
    req: dict,
    session_state,
    *,
    plan_mode_enabled: bool = False,
    user_request: str = "",
    policy: ApprovalPolicy | None = None,
) -> list[dict | None]:
    """Evaluate tool actions against policy and return decisions.

    This is the pre-HITL policy gate. It returns a list where each element is:
    - A decision dict (``{"type": "approve"}`` or ``{"type": "reject", "reason": ...}``)
      when the policy can resolve the action without user input.
    - ``None`` when the action needs user approval (falls through to HITL).

    Args:
        req: The tool action request dict with "action_requests" list
        session_state: Current session state
        plan_mode_enabled: Whether plan mode is active
        user_request: Host-supplied owner intent, separate from tool arguments.
        policy: An already loaded policy for delegated execution, if supplied.

    Returns:
        List of decision dicts or None for each action request
    """
    action_requests = req.get("action_requests", [])
    decisions: list[dict | None] = []

    # Load the approval policy (cached, best-effort)
    try:
        from novacode_cli.security.policy import get_policy
        policy = policy or get_policy()
    except Exception:  # noqa: BLE001
        return [
            {"type": "reject", "reason": "Approval policy unavailable"}
            for _ in action_requests
        ]

    for ar in action_requests:
        tool_name = ar.get("name", "")
        args = ar.get("args", {}) or {}

        # The same plan restrictions apply before policy/auto-approval.
        if plan_mode_enabled:
            reason = plan_tool_rejection(tool_name, args)
            if reason:
                decisions.append({"type": "reject", "reason": reason})
                continue

        # Policy evaluation: allow → approve, deny → reject, ask → None (HITL)
        try:
            verdict = policy.evaluate(tool_name, args)
            tier = verdict.tier if hasattr(verdict, 'tier') else verdict
            if tier not in {"allow", "ask", "deny"}:
                decisions.append({"type": "reject", "reason": "Invalid approval policy verdict"})
                continue
            if tier == "allow":
                decisions.append({"type": "approve"})
                continue
            if tier == "deny":
                decisions.append({"type": "reject", "reason": f"Policy denied: {tool_name}"})
                continue
        except Exception:  # noqa: BLE001 — A broken policy cannot grant execution.
            decisions.append({"type": "reject", "reason": "Approval policy evaluation failed"})
            continue

        # Auto-approve keeps hard denials authoritative. Existing policy allows
        # are still immediate; only actions that normally ask the user are
        # delegated when the opt-in setting is enabled.
        if getattr(session_state, "auto_approve", False):
            if tier == "allow":
                decisions.append({"type": "approve"})
                continue
            try:
                from novacode_cli.config.nova_config import NovaConfig

                config = NovaConfig()
                use_decision_model = config.get_auto_approve_decisions_enabled()
            except Exception:  # noqa: BLE001 — An unreadable preference grants nothing.
                decisions.append(None)
                continue
            if use_decision_model:
                from novacode_cli.agents.approval_decisions import decide_tool_approval

                verdict = decide_tool_approval(
                    tool_name, args, config=config, user_request=user_request
                )
                if verdict["decision"] == "ask":
                    decisions.append(None)
                    continue
                result = {
                    "type": "approve" if verdict["decision"] == "approve" else "reject",
                    "approval_source": verdict["source"],
                }
                if verdict["source"] == "decision_model":
                    result["decision_model"] = config.get_tool_verdict_model()
                result["reason"] = verdict["reason"]
                decisions.append(result)
                continue
            if tier != "deny":
                decisions.append({"type": "approve"})
                continue

        # Check session allow rules (remembered for this session)
        try:
            session_allow = get_session_allow()
            if session_allow.matches(tool_name, args):
                decisions.append({"type": "approve"})
                continue
        except Exception:  # noqa: BLE001
            pass

        # Fall through to HITL
        decisions.append(None)

    return decisions

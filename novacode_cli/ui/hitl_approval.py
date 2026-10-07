"""Human-in-the-loop approval flow for tool actions.

This module provides the shared policy evaluation functions used by the
agent loop, headless runner, and TUI. The REPL-specific approval prompts
have been removed.
"""

from __future__ import annotations


from novacode_cli.config.config import console
from novacode_cli.config.plan_mode import BLOCKED_TOOLS, RESTRICTED_WRITE_TOOLS
from novacode_cli.security.policy import ApprovalPolicy
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
        if tool_name in BLOCKED_TOOLS:
            decisions.append({"type": "reject", "reason": f"Blocked in plan mode: {tool_name}"})
            any_rejected = True
        elif tool_name in RESTRICTED_WRITE_TOOLS:
            # Allow writes to .nova/plans/ and .nova/ralph/ paths
            args = ar.get("args", {}) or {}
            file_path = args.get("file_path", "") or args.get("path", "")
            if file_path and (".nova/plans/" in str(file_path) or ".nova/ralph/" in str(file_path)):
                decisions.append({"type": "approve"})
            else:
                decisions.append({
                    "type": "reject",
                    "message": "writes/edits are only allowed inside .nova/plans/ or .nova/ralph/ in plan mode",
                    "reason": f"Write blocked in plan mode: {tool_name}",
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

    Returns:
        List of decision dicts or None for each action request
    """
    action_requests = req.get("action_requests", [])
    decisions: list[dict | None] = []

    # Load the approval policy (cached, best-effort)
    try:
        from novacode_cli.security.policy import get_policy
        policy = get_policy()
    except Exception:  # noqa: BLE001
        policy = ApprovalPolicy()

    for ar in action_requests:
        tool_name = ar.get("name", "")
        args = ar.get("args", {}) or {}

        # Plan mode: block write tools
        if plan_mode_enabled:
            if tool_name in BLOCKED_TOOLS:
                decisions.append({"type": "reject", "reason": f"Blocked in plan mode: {tool_name}"})
                continue
            if tool_name in RESTRICTED_WRITE_TOOLS:
                decisions.append({"type": "reject", "reason": f"Write blocked in plan mode: {tool_name}"})
                continue

        # Auto-approve: skip all prompts (checked before policy so auto_approve
        # short-circuits even policy-denied actions — the user explicitly opted in).
        if getattr(session_state, "auto_approve", False) and policy.evaluate(tool_name, args).tier != "deny":
            decisions.append({"type": "approve"})
            continue

        # Policy evaluation: allow → approve, deny → reject, ask → None (HITL)
        try:
            verdict = policy.evaluate(tool_name, args)
            tier = verdict.tier if hasattr(verdict, 'tier') else verdict
            if tier == "allow":
                decisions.append({"type": "approve"})
                continue
            if tier == "deny":
                decisions.append({"type": "reject", "reason": f"Policy denied: {tool_name}"})
                continue
        except Exception:  # noqa: BLE001 — never break the turn
            pass

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

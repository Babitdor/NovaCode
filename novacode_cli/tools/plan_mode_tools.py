"""Standalone plan-mode tools for the main agent tool list.

Provides AskUserQuestion, EnterPlanMode, and ExitPlanMode as @tool-decorated
functions so they can be registered directly without requiring middleware.

| Tool             | interrupt() | Permission Required |
|------------------|-------------|---------------------|
| ask_user_question| Yes         | No                  |
| enter_plan_mode  | No          | No                  |
| exit_plan_mode   | Yes         | Yes                 |
"""

from __future__ import annotations

import contextvars
from typing import Any

from langchain.tools import tool
from langgraph.types import interrupt

from novacode_cli.prompts import render_template


_PLAN_MODE_PROMPT = render_template("planning.jinja")

_auto_approve_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_auto_approve_var", default=False
)


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------


def _option_parts(raw: list[str | dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Each option as a ``(label, description, value)`` triple.

    Single source of truth for both wire formats below, so a rich UI's
    structured view and the flat view every other surface receives cannot drift.
    """
    parts: list[tuple[str, str, str]] = []
    for opt in raw:
        if isinstance(opt, dict):
            label = str(opt.get("label") or opt.get("value") or opt)
            description = str(opt.get("description") or "")
            value = str(opt.get("value") or label)
        else:
            label = description = value = str(opt)
        parts.append((label, description, value))
    return parts


def _display(label: str, description: str) -> str:
    """The one-line form shown by the console renderer and the remote bridges."""
    return f"{label} — {description}" if description else label


def _normalize_options(
    raw: list[str | dict[str, Any]],
) -> tuple[list[str], dict[str, str]]:
    """Normalise options to display strings and build a label→value mapping."""
    display_options: list[str] = []
    value_map: dict[str, str] = {}
    for label, description, value in _option_parts(raw):
        display = _display(label, description)
        display_options.append(display)
        value_map[display] = value
    return display_options, value_map


def _structured_choices(raw: list[str | dict[str, Any]]) -> list[dict[str, str]]:
    """Structured option rows for a UI that lays the description out separately.

    ``_normalize_options`` flattens a label and its description into a single
    string, which a rich TUI cannot split apart again. This is additive: the
    flat ``options`` list stays the wire format for the console renderer and the
    remote bridges, and ``display`` is the exact string the flat form carries so
    both surfaces resolve through the same ``value_map``.

    Absent, a UI falls back to the flat strings: the remote bridges, the cowork
    server and any older caller send only ``options``.
    """
    return [
        {
            "label": label,
            "description": description,
            "value": value,
            "display": _display(label, description),
        }
        for label, description, value in _option_parts(raw)
    ]


@tool
def ask_user_question(
    question: str,
    options: list[str | dict[str, Any]],
    context: str | None = None,
) -> str:
    """Ask the user a multiple-choice question and wait for their response.

    Use this when you need clarification or a decision before proceeding.
    Provide at least 2 options — an "Other / free text" option is shown automatically.

    Args:
        question: The question to ask.
        options: At least 2 choices for the user. Each may be a plain string or a
            dict with "label", "value", and optional "description" keys.
        context: Optional one-sentence explanation of why you're asking.

    Returns:
        The user's selected option as a string.
    """
    if not options or len(options) < 2:
        return "Error: At least 2 options are required for ask_user_question."

    display_options, value_map = _normalize_options(options)

    request: dict[str, Any] = {
        "question": question,
        "options": display_options,
        "choices": _structured_choices(options),
        "question_type": "structured",
    }
    if context:
        request["context"] = context

    response = interrupt({"type": "question", "request": request})

    raw_answer = (
        response["answer"] if isinstance(response, dict) and "answer" in response else str(response)
    )
    return value_map.get(raw_answer, raw_answer)


# ---------------------------------------------------------------------------
# EnterPlanMode
# ---------------------------------------------------------------------------


@tool
def enter_plan_mode(reason: str = "") -> str:
    """Switch to plan mode to investigate the codebase and design an approach before coding.

    Call this before any multi-step, complex, or hard-to-reverse task.
    Returns the full planning instructions — read them carefully and follow every phase.

    Args:
        reason: Optional one-sentence description of why planning is needed.

    Returns:
        Planning instructions to follow for the rest of this task.
    """
    header = f"Plan mode activated. Reason: {reason}\n\n" if reason else "Plan mode activated.\n\n"
    return header + _PLAN_MODE_PROMPT


# ---------------------------------------------------------------------------
# ExitPlanMode
# ---------------------------------------------------------------------------


def _mirror_to_global_archive(plan_path: "Path", project_root: "Path") -> None:
    """Copy the saved plan into ``~/.nova/plans/<project>/`` (best-effort).

    Plans live with their project, which makes browsing across checkouts
    impossible. The archive is a read-only mirror for that; the project copy
    stays authoritative, so a failure here must never affect approval.
    """
    try:
        from novacode_cli.plan_archive import mirror_plan

        mirror_plan(plan_path, project_root)
    except Exception:  # noqa: BLE001 - archiving must never fail an approval
        pass


def _persist_approved_plan(plan: str) -> str | None:
    """Save an approved plan to ``.nova/plans/`` named after its title.

    ``# Refactor auth flow`` → ``plan-refactor-auth-flow.md``. Falls back to a
    timestamp name when the plan has no heading; appends a timestamp on
    collision. Returns the saved path (workspace-relative) or None on failure.
    """
    import re
    from datetime import datetime
    from pathlib import Path

    try:
        from novacode_cli.config.config import settings

        root = Path(settings.get_workspace_root())
    except Exception:  # noqa: BLE001
        root = Path.cwd()
    plans_dir = root / ".nova" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    m = re.search(r"^#+\s+(.+)$", plan, re.MULTILINE)
    slug = re.sub(r"[^a-z0-9]+", "-", (m.group(1) if m else "").lower()).strip("-")[:50]
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    name = f"plan-{slug}.md" if slug else f"plan-{stamp}.md"
    path = plans_dir / name
    if path.exists():
        path = plans_dir / f"plan-{slug}-{stamp}.md"
    path.write_text(plan, encoding="utf-8")
    _mirror_to_global_archive(path, root)
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


@tool
def exit_plan_mode(plan: str = "") -> str:
    """Present your completed plan for user approval and exit plan mode.

    Pass your full implementation plan as Markdown in ``plan`` — it is shown to
    the user inline for review (Claude Code plan-mode style), so you do not need
    to write it to a file first. Execution pauses while the user reviews.
    On approval the plan is automatically saved to ``.nova/plans/`` named
    after the plan's title.

    Args:
        plan: The implementation plan as Markdown to present for approval.
            If omitted, the most recent plan written under ``.nova/plans/`` is
            used as a fallback.

    Returns:
        "Plan approved. Proceed with implementation." or
        "Plan rejected. Revise the plan based on user feedback."
    """
    response = interrupt(
        {
            "type": "plan_approval",
            "message": "Plan is ready for review.",
            "plan": plan or "",
        }
    )

    if isinstance(response, dict):
        if response.get("approved"):
            saved = None
            if plan.strip():
                try:
                    saved = _persist_approved_plan(plan)
                except Exception:  # noqa: BLE001 - persistence must never fail approval
                    saved = None
            suffix = f" (Plan saved to {saved})" if saved else ""
            return f"Plan approved. Proceed with implementation.{suffix}"
        action = response.get("action", "refine")
        feedback = response.get("feedback", "")
        feedback_suffix = f"\n\nUser feedback: {feedback}" if feedback else ""
        if action == "refine":
            return f"Plan needs refinement. Continue iterating on the plan.{feedback_suffix}"
        return f"Plan rejected. Revise the plan based on user feedback.{feedback_suffix}"
    return str(response)


__all__ = [
    "ask_user_question",
    "enter_plan_mode",
    "exit_plan_mode",
]

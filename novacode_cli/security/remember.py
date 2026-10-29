"""UI-agnostic glue for applying a 'session' or 'always' remember decision.

Both front-ends (REPL + TUI) call this after the user picks "Allow for session"
or confirms "Always allow…", so the synthesize/persist logic lives in one place
and is testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from novacode_cli.security import policy_writer
from novacode_cli.security.rule_synthesis import ProposedRule, synthesize_rule
from novacode_cli.security.session_allow import get_session_allow

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class RememberResult:
    """Outcome of :func:`apply_remember` for the UI to display.

    ``saved_path`` is the file written for a successful ``"always"`` persist, or
    ``None`` for a session-only result *or* a persist that failed. ``error`` is
    set only when an ``"always"`` persist failed (the rule is still active for
    the session); the UI should then show a "kept for this session" notice.
    """

    rule: ProposedRule
    saved_path: Path | None  # None for session-only or a failed persist
    error: str | None = None


def apply_remember(
    kind: str,
    tool_name: str,
    args: dict | None,
    *,
    target: str | None = None,
    project_root: Path | None = None,
    rule: ProposedRule | None = None,
    session: str | None = None,
) -> RememberResult:
    """Apply a remember decision.

    Args:
        kind: ``"session"`` (in-memory) or ``"always"`` (persist + in-memory).
        tool_name: The approved tool's name.
        args: The approved tool call's arguments (used to synthesize a rule).
        target: For ``"always"``, ``"project"`` (default) or ``"global"``.
        project_root: Project root for a project-target write.
        rule: An already-synthesized (possibly user-edited) rule; when given,
            synthesis is skipped.
        session: Host-provided runtime session key for UI callbacks.

    Returns:
        A :class:`RememberResult`.

    Raises:
        ValueError: If ``kind`` is not ``"session"`` or ``"always"``.
    """
    rule = rule or synthesize_rule(tool_name, args)
    if kind == "session":
        get_session_allow(session).add(rule)
        return RememberResult(rule=rule, saved_path=None)
    if kind == "always":
        # Remember for the session FIRST so a failed file write still benefits
        # the current run — the rule must take effect even if persistence fails,
        # and a write error must never crash the turn (degrade to session-only).
        get_session_allow(session).add(rule)
        try:
            path = policy_writer.append_rule(
                rule, target=target or "project", project_root=project_root
            )
        except OSError as exc:
            return RememberResult(rule=rule, saved_path=None, error=str(exc))
        return RememberResult(rule=rule, saved_path=path)
    msg = f"unknown remember kind: {kind!r}"
    raise ValueError(msg)

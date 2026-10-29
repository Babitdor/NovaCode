"""In-memory 'allow for this session' layer for tool approvals.

Holds generalized rules the user accepted for one project/thread (cleared on
session close). Standalone callers retain a process-local list. Matching is
delegated to a throwaway :class:`ApprovalPolicy` built
from the session rules, so session and persisted rules match identically — and,
crucially, the policy's built-in deny/dangerous checks still apply, so a session
rule can never allow a denied command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.security.policy import ApprovalPolicy, load_policy
from novacode_cli.session.runtime_identity import current_identity

if TYPE_CHECKING:
    from novacode_cli.security.rule_synthesis import ProposedRule


class SessionAllowList:
    """A process-lifetime list of user-accepted allow rules."""

    def __init__(self) -> None:
        """Initialise an empty session allow-list."""
        self._rules: list[ProposedRule] = []
        self._policy: ApprovalPolicy | None = None

    def add(self, rule: ProposedRule) -> None:
        """Append a rule and invalidate the cached matcher."""
        self._rules.append(rule)
        self._policy = None

    def _build(self) -> ApprovalPolicy:
        # Borrow only the *deny* lists from the loaded policy so that built-in
        # hard-deny rules (e.g. ``sudo``, system paths, metadata endpoints) are
        # always active — session rules can never override them.  Allow lists
        # are built solely from what the user has explicitly accepted this
        # session, so a ``npm run build`` approval does not accidentally also
        # allow ``npm test`` (which the base policy's allow list would cover).
        base = load_policy()
        tool_tiers: dict[str, str] = {}
        shell_allow: list[str] = []
        path_allow: list[str] = []
        domain_allow: list[str] = []
        for r in self._rules:
            if r.category == "shell":
                shell_allow.append(r.value)
            elif r.category == "paths":
                path_allow.append(r.value)
            elif r.category == "domains":
                domain_allow.append(r.value)
            elif r.category == "tool":
                tool_tiers[r.tool_name] = "allow"
        return ApprovalPolicy(
            tool_tiers=tool_tiers,  # type: ignore[arg-type]
            shell_allow=shell_allow,
            shell_deny=base.shell_deny,
            path_allow=path_allow,
            path_deny=base.path_deny,
            domain_allow=domain_allow,
            domain_deny=base.domain_deny,
        )

    def matches(self, tool_name: str, args: dict | None) -> bool:
        """True if a session rule allows this call (deny/dangerous still win)."""
        if not self._rules:
            return False
        if self._policy is None:
            self._policy = self._build()
        return self._policy.evaluate(tool_name, args or {}).allowed

    def clear(self) -> None:
        """Forget all session rules."""
        self._rules.clear()
        self._policy = None


_SESSION_ALLOW: SessionAllowList | None = None
_SESSION_ALLOWS: dict[str, SessionAllowList] = {}


def get_session_allow(session: str | None = None) -> SessionAllowList:
    """Return the invoking session's rules, or the standalone caller's rules."""
    global _SESSION_ALLOW  # noqa: PLW0603 — module-level process singleton
    identity = current_identity.get()
    session = session or (identity.session if identity else None)
    if session is not None:
        if session not in _SESSION_ALLOWS:
            _SESSION_ALLOWS[session] = SessionAllowList()
        return _SESSION_ALLOWS[session]
    if _SESSION_ALLOW is None:
        _SESSION_ALLOW = SessionAllowList()
    return _SESSION_ALLOW


def reset_session_allow() -> None:
    """Drop the session allow-list (used by tests)."""
    global _SESSION_ALLOW  # noqa: PLW0603 — module-level process singleton
    _SESSION_ALLOW = None
    _SESSION_ALLOWS.clear()


def release_session_allow(session: str) -> None:
    """Forget grants when their owning session ends."""
    _SESSION_ALLOWS.pop(session, None)


__all__ = [
    "SessionAllowList",
    "get_session_allow",
    "reset_session_allow",
]

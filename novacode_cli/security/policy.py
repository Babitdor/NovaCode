"""Risk-tiered approval policy engine.

Generalizes :mod:`novacode_cli.git_safety` into a tool-agnostic classifier that
resolves each tool call to one of three tiers — ``allow`` (run, no prompt),
``ask`` (the existing HITL prompt), or ``deny`` (block, never promptable). This
is the pre-HITL gate: the safe majority runs silently, the dangerous minority is
blocked outright, and only the ambiguous middle interrupts the user.

The policy is **config-driven**. Built-in defaults are merged with
``~/.nova/approval-policy.json`` and an optional project
``<root>/.nova/approval-policy.json``. User/project config can *add* allow/deny
rules and override per-tool default tiers, but the built-in ``deny`` rules are
always applied (deny is checked before any tier fallback), so a hard deny can
never be loosened away.

Precedence inside :meth:`ApprovalPolicy.evaluate` is **deny → ask → allow →
per-tool default**, with deny winning first for security.

Path note: in local mode NOVA uses virtual ``/``-rooted (workspace-relative)
paths, so writes are already sandboxed to the workspace. We therefore keep
``write_file``/``edit_file`` at the ``ask`` default and only *deny* paths that
match system/secret globs (meaningful for real absolute paths); auto-allowing
writes is opt-in via config.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from re import Pattern
from re import compile as _re_compile
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

if TYPE_CHECKING:
    from pathlib import Path

from novacode_cli.config.config import HOME_DIR
from novacode_cli.git_safety import (
    BLOCKED_COMMANDS,
    DANGEROUS_GIT_COMMANDS,
)

logger = logging.getLogger(__name__)

Tier = Literal["allow", "ask", "deny"]

# ---------------------------------------------------------------------------
# Tool groupings — which family of rules a tool is evaluated against.
# ---------------------------------------------------------------------------
# Tools whose ``command`` argument is a shell command, and so must be judged by
# the same allow/deny rules. ``daemon`` belongs here for the reason the others
# do — it runs its command with shell=True — and doubly so because the process
# it starts outlives the session. Leaving it out let an agent run through
# `daemon(action="start", command=...)` exactly what `shell(command=...)` would
# have denied. Actions without a command (list/status/logs) carry no command to
# judge and fall through to the tool default.
_SHELL_TOOLS = frozenset(
    {"shell", "execute", "run_tests", "start_dev_server", "daemon"}
)
#: `daemon` actions that only read state — they start no process and kill
#: none, so gating them buys nothing and costs approval fatigue.
_DAEMON_READONLY_ACTIONS = frozenset({"list", "status", "logs"})

_PATH_TOOLS = frozenset({"write_file", "edit_file"})
_URL_TOOLS = frozenset({"fetch_url"})

# Per-tool default tier (the fallback when no specific rule matches).
_DEFAULT_TOOL_TIERS: dict[str, Tier] = {
    "shell": "ask",
    "execute": "ask",
    "write_file": "ask",
    "edit_file": "ask",
    "fetch_url": "ask",
    "run_tests": "ask",
    "start_dev_server": "ask",
    "write_memory": "ask",
    "web_search": "allow",
    "docs_search": "allow",
    "duckduckgo_search": "allow",
}

# Built-in shell allowlist — safe, read-only-ish commands that should run silently.
_DEFAULT_SHELL_ALLOW: list[str] = [
    r"^\s*git\s+(status|diff|log|show|branch\b|remote\s+-v|rev-parse|describe|blame|shortlog)",
    r"^\s*ls\b",
    r"^\s*pwd\b",
    r"^\s*cat\b",
    r"^\s*echo\b",
    r"^\s*head\b",
    r"^\s*tail\b",
    r"^\s*wc\b",
    r"^\s*grep\b",
    r"^\s*rg\b",
    r"^\s*find\b",
    r"^\s*which\b",
    r"^\s*pytest\b",
    r"^\s*ruff\b",
    r"^\s*mypy\b",
    r"^\s*uv\s+run\b",
    r"^\s*npm\s+(test|run\s+test)\b",
    r"^\s*node\s+--version\b",
    r"^\s*python\s+--version\b",
]

# Built-in shell denylist — destructive or privilege-escalating commands.
_DEFAULT_SHELL_DENY: list[str] = [
    r"\brm\s+-[a-z]*r[a-z]*f?\s+[\"']?[/~]",  # rm -rf / or ~
    r"\brm\s+-[a-z]*f[a-z]*r\s+[\"']?[/~]",  # rm -fr / variant
    r":\s*\(\s*\)\s*\{",  # fork bomb :(){
    r"\bsudo\b",  # privilege escalation
    r"\bmkfs\b",  # format a filesystem
    r"\bdd\s+if=",  # raw disk write
    r">\s*/dev/sd[a-z]",  # overwrite a block device
    r"\bchmod\s+(-R\s+)?777\b",  # world-writable
    r"\bchmod\s+(-R\s+)?a\+rwx\b",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b",  # curl ... | sh
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\.",
]

# Built-in path denylist — system + secret locations a write must never touch.
# (Meaningful for real absolute paths; virtual workspace paths never match.)
_DEFAULT_PATH_DENY: list[str] = [
    "/etc/**",
    "/boot/**",
    "/sys/**",
    "/proc/**",
    "/usr/**",
    "/bin/**",
    "/sbin/**",
    "**/.ssh/**",
    "**/.ssh",
    "**/.aws/credentials",
    "**/.gnupg/**",
]

# Built-in domain denylist — cloud-metadata / SSRF-prone hosts.
_DEFAULT_DOMAIN_DENY: list[str] = [
    "169.254.169.254",
    "metadata.google.internal",
]


# ---------------------------------------------------------------------------
# Decision + policy types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalDecision:
    """The resolved tier for a single tool call.

    Attributes:
        tier: ``allow`` | ``ask`` | ``deny``.
        reason: Human-readable justification (shown in prompts / rejections).
        rule: The rule/pattern that matched (for audit logging).
    """

    tier: Tier
    reason: str = ""
    rule: str = ""

    @property
    def allowed(self) -> bool:
        """True when the call may run without a prompt."""
        return self.tier == "allow"

    @property
    def denied(self) -> bool:
        """True when the call must be blocked without asking."""
        return self.tier == "deny"


@dataclass
class ApprovalPolicy:
    """A compiled, queryable approval policy.

    Build via :func:`load_policy` (merges defaults with config) rather than
    constructing directly, except in tests.
    """

    tool_tiers: dict[str, Tier]
    shell_allow: list[str]
    shell_deny: list[str]
    path_allow: list[str]
    path_deny: list[str]
    domain_allow: list[str]
    domain_deny: list[str]

    _shell_allow_re: list[Pattern[str]] = field(default_factory=list, repr=False)
    _shell_deny_re: list[Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """Pre-compile the shell allow/deny regex lists."""
        self._shell_allow_re = _compile_all(self.shell_allow)
        self._shell_deny_re = _compile_all(self.shell_deny)

    # -- introspection ------------------------------------------------------

    def tool_default(self, tool_name: str) -> Tier:
        """Per-tool default tier (``ask`` for unknown tools)."""
        return self.tool_tiers.get(tool_name, "ask")

    def has_arg_rules(self, tool_name: str) -> bool:
        """True when the tool's decision depends on its arguments.

        Such tools must stay gated even if their default tier is ``allow``,
        because a specific call may still ``ask``/``deny``.
        """
        return tool_name in _SHELL_TOOLS or tool_name in _PATH_TOOLS or tool_name in _URL_TOOLS

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, tool_name: str, args: dict[str, Any]) -> ApprovalDecision:
        """Resolve a tool call to an :class:`ApprovalDecision`."""
        args = args or {}
        if tool_name in _SHELL_TOOLS:
            return self._eval_shell(tool_name, args)
        if tool_name in _PATH_TOOLS:
            return self._eval_path(tool_name, args)
        if tool_name in _URL_TOOLS:
            return self._eval_url(tool_name, args)
        return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")

    def _eval_shell(self, tool_name: str, args: dict[str, Any]) -> ApprovalDecision:
        # `daemon` reaches here for every action, but only `start` carries a
        # command. Its read-only actions execute nothing, and prompting for
        # them would train the user to blanket-approve the tool — which is how
        # a gate stops protecting the action that matters. `stop` is NOT here:
        # it force-kills a process tree.
        if tool_name == "daemon":
            action = str(args.get("action") or "").strip().lower()
            if action in _DAEMON_READONLY_ACTIONS:
                return ApprovalDecision("allow", rule="daemon-readonly")

        command = str(args.get("command") or "").strip()
        if not command:
            return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")

        # NOTE: we deliberately do NOT treat shell metacharacters (``;`` ``&&``
        # ``||`` ``$(...)`` backticks) as "injection" here. They are normal shell
        # syntax (``pkill -f x 2>/dev/null; echo done``, ``... || true``), and a
        # general shell tool must allow them. Genuinely dangerous commands hidden
        # inside a chain are still caught below: the deny regexes ``.search()`` the
        # whole string, so ``ls; rm -rf /`` still matches the ``rm -rf /`` rule.
        low = command.lower()
        # deny: blocked git operations (config edits, --no-verify, ...)
        for blocked, why in BLOCKED_COMMANDS.items():
            if blocked.lower() in low:
                return ApprovalDecision("deny", f"Blocked: {why}", blocked)
        # deny: built-in + configured destructive patterns
        for pat in self._shell_deny_re:
            if pat.search(command):
                return ApprovalDecision("deny", "Matches a denied command pattern", pat.pattern)
        # ask: dangerous-but-sometimes-legitimate git operations
        for dangerous, why in DANGEROUS_GIT_COMMANDS.items():
            if dangerous.lower() in low:
                return ApprovalDecision("ask", why, dangerous)
        # allow: explicit allowlist
        for pat in self._shell_allow_re:
            if pat.search(command):
                return ApprovalDecision("allow", "Matches an allowed command", pat.pattern)
        # fallback
        return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")

    def _eval_path(self, tool_name: str, args: dict[str, Any]) -> ApprovalDecision:
        raw = str(args.get("file_path") or "")
        if not raw:
            return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")
        norm = raw.replace("\\", "/")
        for glob in self.path_deny:
            if _glob_match(norm, glob):
                return ApprovalDecision("deny", f"Path matches a denied glob: {glob}", glob)
        for glob in self.path_allow:
            if _glob_match(norm, glob):
                return ApprovalDecision("allow", "Path matches an allowed glob", glob)
        return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")

    def _eval_url(self, tool_name: str, args: dict[str, Any]) -> ApprovalDecision:
        url = str(args.get("url") or "")
        host = _extract_host(url)
        if not host:
            return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")
        for dom in self.domain_deny:
            if _host_matches(host, dom):
                return ApprovalDecision("deny", f"Domain is denied: {dom}", dom)
        for dom in self.domain_allow:
            if _host_matches(host, dom):
                return ApprovalDecision("allow", "Domain is allowed", dom)
        return ApprovalDecision(self.tool_default(tool_name), rule="tool-default")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compile_all(patterns: list[str]) -> list[Pattern[str]]:
    compiled: list[Pattern[str]] = []
    for pat in patterns:
        try:
            compiled.append(_re_compile(pat))
        except Exception:  # noqa: BLE001 — a bad config regex must not break the gate
            logger.warning("Skipping invalid policy regex: %r", pat)
    return compiled


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch-based glob match that also lets ``/etc/**`` cover ``/etc``."""
    if fnmatch.fnmatch(path, pattern):
        return True
    base = pattern.rstrip("/*")
    return bool(base) and (path == base or path.startswith(base + "/"))


def _extract_host(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        return (parsed.hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _host_matches(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _config_files(project_root: Path | None) -> list[Path]:
    files = [HOME_DIR / "approval-policy.json"]
    if project_root is not None:
        files.append(project_root / ".nova" / "approval-policy.json")
    return files


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — config is best-effort; never raise
        logger.warning("Could not read approval policy at %s", path, exc_info=True)
        return {}


def load_policy(project_root: Path | None = None) -> ApprovalPolicy:
    """Build an :class:`ApprovalPolicy` from built-in defaults ⊕ config files.

    Args:
        project_root: Project root for the optional project policy file. Defaults
            to the current working directory.

    Returns:
        A compiled policy. Always succeeds — config errors are logged and skipped.
    """
    if project_root is None:
        # The project root, not the launch directory. With Path.cwd() here, a
        # project's own .nova/approval-policy.json silently stopped applying
        # the moment Nova was started from a subfolder — its deny rules became
        # "ask", with nothing to say so. Every get_policy() caller (the live
        # HITL gate among them) passes no root, so this default is the gate.
        from novacode_cli.config.config import settings

        project_root = settings.get_workspace_root()

    tool_tiers: dict[str, Tier] = dict(_DEFAULT_TOOL_TIERS)
    shell_allow = list(_DEFAULT_SHELL_ALLOW)
    shell_deny = list(_DEFAULT_SHELL_DENY)
    path_allow: list[str] = []
    path_deny = list(_DEFAULT_PATH_DENY)
    domain_allow: list[str] = []
    domain_deny = list(_DEFAULT_DOMAIN_DENY)

    for cfg in _config_files(project_root):
        data = _read_json(cfg)
        if not data:
            continue
        tool_tiers.update(
            {
                tool: tier
                for tool, tier in (data.get("tools") or {}).items()
                if tier in ("allow", "ask", "deny")
            }
        )
        shell = data.get("shell") or {}
        shell_allow += list(shell.get("allow", []))
        shell_deny += list(shell.get("deny", []))
        paths = data.get("paths") or {}
        path_allow += list(paths.get("allow", []))
        path_deny += list(paths.get("deny", []))
        domains = data.get("domains") or {}
        domain_allow += list(domains.get("allow", []))
        domain_deny += list(domains.get("deny", []))

    return ApprovalPolicy(
        tool_tiers=tool_tiers,
        shell_allow=shell_allow,
        shell_deny=shell_deny,
        path_allow=path_allow,
        path_deny=path_deny,
        domain_allow=domain_allow,
        domain_deny=domain_deny,
    )


_CACHED_POLICY: ApprovalPolicy | None = None


def get_policy(project_root: Path | None = None, *, refresh: bool = False) -> ApprovalPolicy:
    """Return a process-cached policy, loading it on first use.

    Args:
        project_root: Passed through to :func:`load_policy` on (re)load.
        refresh: Force a reload (e.g. after the config file changes, or in tests).
    """
    global _CACHED_POLICY  # noqa: PLW0603 — module-level process cache
    if _CACHED_POLICY is None or refresh:
        _CACHED_POLICY = load_policy(project_root)
    return _CACHED_POLICY


def reset_policy_cache() -> None:
    """Drop the cached policy (used by tests)."""
    global _CACHED_POLICY  # noqa: PLW0603 — module-level process cache
    _CACHED_POLICY = None


__all__ = [
    "ApprovalDecision",
    "ApprovalPolicy",
    "Tier",
    "get_policy",
    "load_policy",
    "reset_policy_cache",
]

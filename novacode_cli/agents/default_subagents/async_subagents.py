# Async Subagents for NOVA CLI
# These run on remote LangGraph servers in the background

import logging
import os
import socket
from urllib.parse import urlsplit

from deepagents.middleware.async_subagents import AsyncSubAgent

logger = logging.getLogger(__name__)

# ── Base URL resolution ────────────────────────────────────────────────────────

#: Where the async-subagent LangGraph server lives when nothing says otherwise.
#: Every async graph is hosted by the one ``novacode`` container; routing is by
#: ``graph_id``, so a single base URL and port cover all of them.
DEFAULT_ASYNC_AGENT_BASE_URL = "http://localhost"
ASYNC_AGENT_PORT = 2024

#: How long to wait for the server's port to accept a connection. This runs on
#: the agent-build path, so it has to be short enough not to be felt; a
#: container that is up answers a local TCP connect in single-digit ms.
_PROBE_TIMEOUT_S = 0.4

_availability: bool | None = None


def _resolve_async_agent_url(port: int) -> str:
    """Resolve the LangGraph server URL for an async subagent.

    Priority:
    1. ``ASYNC_AGENT_BASE_URL`` environment variable (shared base, e.g.
       ``http://localhost`` or ``http://doc-agent`` in Docker)
    2. ``LANGGRAPH_API_URL`` environment variable (shared LangGraph Platform URL)
    3. :data:`DEFAULT_ASYNC_AGENT_BASE_URL`

    Previously this returned ``None`` when neither variable was set, which made
    every spec fall back to an in-process ASGI transport that only exists inside
    a ``langgraph dev`` process — so with the container running but no env var
    exported, delegation failed. The port is appended as ``{base}:{port}``.
    """
    base = (
        os.environ.get("ASYNC_AGENT_BASE_URL")
        or os.environ.get("LANGGRAPH_API_URL")
        or DEFAULT_ASYNC_AGENT_BASE_URL
    ).rstrip("/")
    # A base that already names a port wins — writing the obvious
    # ASYNC_AGENT_BASE_URL=http://localhost:2024 otherwise produced
    # "http://localhost:2024:2024", which no client can reach.
    if _has_port(base):
        return base
    return f"{base}:{port}"


def _has_port(base: str) -> bool:
    """Whether *base* already carries an explicit ``:port``."""
    try:
        return urlsplit(base).port is not None
    except ValueError:
        return False


def async_agents_available(*, refresh: bool = False) -> bool:
    """Whether the async-subagent server is actually reachable.

    The async subagents only exist while the ``novacode`` LangGraph container is
    running. Offering them when it is not turns every delegation into a failed
    round-trip, so the specs are withheld instead and the agent uses its
    ordinary in-process subagents.

    One TCP connect, cached for the process: the container does not come and go
    mid-session, and this sits on the agent-build path.
    """
    global _availability
    if _availability is not None and not refresh:
        return _availability
    try:
        parts = urlsplit(_resolve_async_agent_url(ASYNC_AGENT_PORT))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            # A base URL the client cannot use. Reaching some other server on
            # the default port would be worse than reporting it unavailable:
            # the specs would carry the unusable URL.
            msg = f"unusable async agent base URL: {parts.geturl()!r}"
            raise ValueError(msg)
        host, port = parts.hostname, parts.port or ASYNC_AGENT_PORT
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            _availability = True
    except (OSError, ValueError):  # unreachable, or a malformed base URL
        host = port = "?"
        _availability = False
    logger.debug("async subagent server at %s:%s reachable=%s", host, port, _availability)
    return _availability


def _resolve_async_agent_headers() -> dict[str, str]:
    """Resolve auth headers for the async subagent server.

    Uses ``LANGGRAPH_API_KEY`` if set, otherwise returns empty headers
    (assumes local/unauthenticated server).
    """
    api_key = os.environ.get("LANGGRAPH_API_KEY")
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


# ── Agent descriptions ─────────────────────────────────────────────────────────

DOCUMENTATION_UPDATE_AGENT_DESCRIPTION = """An async agent that automatically updates documentation in the background.

Use this agent when:
- You've committed code changes that need documentation updates
- README files need to reflect new features or API changes
- Changelog entries need to be generated from commit messages
- API documentation needs to be synchronized with code changes
- Code comments or docstrings need updating

The agent runs remotely and asynchronously, so you can continue working while it processes documentation updates.
"""

CODE_REVIEW_AGENT_DESCRIPTION = """An async agent that reviews code changes in the background.

Use this agent when:
- You need a code review on uncommitted or recently committed changes
- You want to catch bugs, security issues, or style problems before PR
- You need a second opinion on code quality
- You want structured feedback with severity ratings

The agent analyzes diffs, reads full file context, and produces a structured review report.
"""

TEST_GENERATION_AGENT_DESCRIPTION = """An async agent that generates and maintains test suites in the background.

Use this agent when:
- You need tests for new or modified code
- You want to increase test coverage
- You need to verify existing tests still pass after changes
- You want edge case and error condition coverage

The agent analyzes source code, checks existing tests, generates pytest suites, and verifies they pass.
"""

DEPENDENCY_AUDIT_AGENT_DESCRIPTION = """An async agent that audits project dependencies in the background.

Use this agent when:
- You want to check for outdated packages
- You need a security vulnerability scan (CVEs)
- You want to review the dependency tree
- You need upgrade recommendations with compatibility notes

The agent checks pyproject.toml, runs pip-audit, and produces a structured report with severity levels.
"""

REFACTORING_AGENT_DESCRIPTION = """An async agent that analyzes and improves code quality in the background.

Use this agent when:
- You want to identify code smells and technical debt
- You need linting analysis across the codebase
- You want targeted refactoring suggestions
- You need to reduce complexity or duplication

The agent scans files, runs linters, analyzes structure, and proposes incremental improvements.
"""

PLAN_SCOUT_AGENT_DESCRIPTION = """An async agent that scans the directory and reports planning-relevant findings.

Use this agent when you are PLANNING a change and need to parallelize
codebase investigation:
- You need to map which files exist in an area of the repository
- You need summaries of key files (headers, exports, wiring) to ground a plan
- You need to locate references to a symbol/feature across the tree
- You want several independent areas scanned in parallel before you synthesize a plan

The agent is strictly read-only: it inspects files and returns a structured
findings report to the planner. It never edits, creates, or deletes anything.
"""


# ── Agent builders ─────────────────────────────────────────────────────────────

def _build_agent_spec(
    name: str,
    graph_id: str,
    description: str,
    port: int,
) -> AsyncSubAgent:
    """Build an AsyncSubAgent spec with resolved URL and headers."""
    return {
        "name": name,
        "description": description,
        "graph_id": graph_id,
        "url": _resolve_async_agent_url(port),
        "headers": _resolve_async_agent_headers(),
    }


def build_documentation_update_agent() -> AsyncSubAgent:
    """Build the documentation update async subagent config."""
    return _build_agent_spec(
        name="documentation-update-agent",
        graph_id="documentation-update-agent",
        description=DOCUMENTATION_UPDATE_AGENT_DESCRIPTION,
        port=2024,
    )


def build_code_review_agent() -> AsyncSubAgent:
    """Build the code review async subagent config."""
    return _build_agent_spec(
        name="code-review-agent",
        graph_id="code-review-agent",
        description=CODE_REVIEW_AGENT_DESCRIPTION,
        port=2024,  # one shared server hosts all graphs; routing is by graph_id
    )


def build_test_generation_agent() -> AsyncSubAgent:
    """Build the test generation async subagent config."""
    return _build_agent_spec(
        name="test-generation-agent",
        graph_id="test-generation-agent",
        description=TEST_GENERATION_AGENT_DESCRIPTION,
        port=2024,  # one shared server hosts all graphs; routing is by graph_id
    )


def build_dependency_audit_agent() -> AsyncSubAgent:
    """Build the dependency audit async subagent config."""
    return _build_agent_spec(
        name="dependency-audit-agent",
        graph_id="dependency-audit-agent",
        description=DEPENDENCY_AUDIT_AGENT_DESCRIPTION,
        port=2024,  # one shared server hosts all graphs; routing is by graph_id
    )


def build_refactoring_agent() -> AsyncSubAgent:
    """Build the refactoring async subagent config."""
    return _build_agent_spec(
        name="refactoring-agent",
        graph_id="refactoring-agent",
        description=REFACTORING_AGENT_DESCRIPTION,
        port=2024,  # one shared server hosts all graphs; routing is by graph_id
    )


def build_plan_scout_agent() -> AsyncSubAgent:
    """Build the plan-scout async subagent config.

    The plan agent dispatches scouts in parallel during investigation, then
    synthesizes their findings reports into the plan.
    """
    return _build_agent_spec(
        name="plan-scout-agent",
        graph_id="plan-scout-agent",
        description=PLAN_SCOUT_AGENT_DESCRIPTION,
        port=2024,  # one shared server hosts all graphs; routing is by graph_id
    )


def retrieve_async_subagents() -> list[AsyncSubAgent]:
    """Return the async subagents, or ``[]`` when their server is not running.

    Async subagents run on remote Agent Protocol servers and execute in the
    background, returning a task ID immediately. That server is the ``novacode``
    Docker container; with it stopped the tools would still be advertised and
    every delegation would fail, so nothing is returned and the agent falls back
    to its synchronous in-process subagents.

    Returns:
        List of AsyncSubAgent configurations, empty when the server is down.
    """
    if not async_agents_available():
        logger.info(
            "Async subagent server unreachable — using synchronous subagents. "
            "Start the novacode container to enable background delegation."
        )
        return []
    return [
        build_documentation_update_agent(),
        build_code_review_agent(),
        build_test_generation_agent(),
        build_dependency_audit_agent(),
        build_refactoring_agent(),
        build_plan_scout_agent(),
    ]

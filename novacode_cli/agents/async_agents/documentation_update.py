"""Documentation Update Agent — async subagent for LangGraph Platform.

Runs as a background LangGraph server (referenced by ``langgraph.json``).
Automatically updates project documentation based on code changes and commits:

- Reads commit messages and diffs to detect what changed
- Updates README files to reflect new features or API changes
- Generates and maintains changelog entries
- Synchronizes API documentation with code changes
- Updates code comments and docstrings when appropriate

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

import subprocess
from typing import Any

from deepagents import create_deep_agent
from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
from deepagents.backends import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.store import StoreBackend
from langchain_core.tools import tool
from novacode_cli.agents.async_workspace import workspace_root
from novacode_cli.agents.async_context import (
    AsyncModelOverrideMiddleware,
    NovaAsyncContext,
)
from novacode_cli.config.model_create import build_async_agent_model
from langgraph.store.memory import InMemoryStore

# ── Tools ──────────────────────────────────────────────────────────────────────


@tool
def get_recent_commits(count: int = 10) -> str:
    """Get the most recent commit messages and hashes from the project.

    Useful for understanding what changed since the last documentation update.

    Args:
        count: Number of recent commits to retrieve (default 10).

    Returns:
        Formatted commit log with hash, author, date, and message.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"-{count}", "--pretty=format:%h %an %ad %s", "--date=short"],
            capture_output=True,
            text=True,
            cwd=workspace_root(),
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout or "No commits found."
        return f"Git error: {result.stderr}"
    except FileNotFoundError:
        return "Git is not available in this environment."
    except subprocess.TimeoutExpired:
        return "Git log timed out."
    except Exception as e:
        return f"Error reading git log: {e}"


@tool
def get_commit_diff(commit_hash: str) -> str:
    """Get the full diff for a specific commit.

    Use this to understand exactly what files changed and how.

    Args:
        commit_hash: The commit hash (full or abbreviated).

    Returns:
        The full diff output for the given commit.
    """
    try:
        result = subprocess.run(
            ["git", "show", commit_hash, "--stat", "--patch"],
            capture_output=True,
            text=True,
            cwd=workspace_root(),
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout or "No diff available."
        return f"Git error: {result.stderr}"
    except FileNotFoundError:
        return "Git is not available in this environment."
    except subprocess.TimeoutExpired:
        return "Git diff timed out."
    except Exception as e:
        return f"Error reading diff: {e}"


@tool
def get_changed_files_since(tag_or_hash: str = "HEAD~1") -> str:
    """List files that have changed since a given reference.

    Useful for identifying which documentation files may need updating.

    Args:
        tag_or_hash: Git reference (tag, commit hash, or relative like HEAD~5).

    Returns:
        List of changed files with change type (modified/added/deleted).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", tag_or_hash],
            capture_output=True,
            text=True,
            cwd=workspace_root(),
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout or "No changes found."
        return f"Git error: {result.stderr}"
    except FileNotFoundError:
        return "Git is not available in this environment."
    except subprocess.TimeoutExpired:
        return "Git diff timed out."
    except Exception as e:
        return f"Error listing changed files: {e}"


# ── Agent Configuration ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Documentation Update Agent that runs asynchronously in the background.

Your purpose is to keep project documentation accurate and up-to-date by:

1. **Detecting changes** — Use git tools to find recent commits and diffs
2. **Analyzing impact** — Determine which documentation files need updating
3. **Updating docs** — Read existing docs, make targeted edits, write improvements
4. **Maintaining changelogs** — Add entries for new features, fixes, and breaking changes
5. **Verifying accuracy** — Ensure docs match the actual code behavior

## Workflow

When asked to update documentation:

1. First, check recent commits with `get_recent_commits` to understand what changed
2. Get the diff for relevant commits with `get_commit_diff`
3. List existing documentation files with `glob` (e.g. `glob("**/*.md")`)
4. Read the relevant doc files that need updating with `read_file`
5. Make targeted edits — don't rewrite entire files unless necessary
6. Verify your changes are consistent and accurate

## Guidelines

- Be concise and precise in documentation updates
- Preserve existing tone and style of the project's docs
- Don't add documentation for things that don't exist yet
- When in doubt, check the actual code before writing about it
- For changelogs, follow the project's existing format (keepachangelog.com recommended)
- Never remove deprecation notices or migration guides
"""


def _build_backend() -> CompositeBackend:
    """Build a CompositeBackend with filesystem and persistent store routes.

    The agent needs:
    - Filesystem access to read/write project files
    - Persistent store for tracking what was last updated
    """
    root = workspace_root()

    # Default: filesystem access to the project
    default_backend = FilesystemBackend(
        root_dir=str(root),
        virtual_mode=True,
    )

    # Persistent store for tracking documentation state across runs
    store = InMemoryStore()
    store_backend = StoreBackend(
        store=store,
        namespace=lambda rt: ("documentation-agent", "store"),
    )

    return CompositeBackend(
        default=default_backend,
        routes={
            "/store/": store_backend,
        },
    )


def _resolve_model() -> Any:
    """Resolve this graph's chat model.

    Provider and model come from the environment (``ASYNC_AGENT_PROVIDER``,
    ``ASYNC_AGENT_MODEL``, falling back to ``DOC_AGENT_MODEL``); Ollama stays the
    default. See :func:`novacode_cli.config.model_create.build_async_agent_model`.

    A *dispatch* may name the model to run on instead, by way of the run's context (see
    :mod:`novacode_cli.agents.async_context`). This stays the last word when no context
    arrives, so a deployment driven only by ``.env`` is unaffected.
    """
    return build_async_agent_model()


def _build_agent() -> Any:
    """Build and return the compiled documentation update agent.

    Uses ``create_deep_agent`` from the deepagents framework with:
    - Custom tools for git operations and file discovery
    - Filesystem backend for reading/writing project files
    - In-memory checkpointing for conversation state
    - A focused system prompt for documentation work
    """
    tools = [
        get_recent_commits,
        get_commit_diff,
        get_changed_files_since,
    ]

    backend = _build_backend()
    model = _resolve_model()

    agent = create_deep_agent(
        name="documentation-update-agent",
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        # Let a dispatch name the model to run on (see novacode_cli.agents.async_context).
        # The environment still decides when a run arrives without a context.
        middleware=[AsyncModelOverrideMiddleware(), DelegatedApprovalMiddleware()],
        context_schema=NovaAsyncContext,
    )

    return agent


# ── LangGraph Platform Export ──────────────────────────────────────────────────
# ``langgraph.json`` references this module's ``graph`` attribute.
# LangGraph Platform loads it to serve the agent via its API.

graph = _build_agent()

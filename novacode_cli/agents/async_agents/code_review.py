"""Code Review Agent — async subagent for background code review.

Runs as a background LangGraph server. Reviews code changes, detects bugs,
security issues, and suggests improvements before they reach production.

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

import subprocess
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.tools import tool
from novacode_cli.agents.async_workspace import workspace_root
from novacode_cli.agents.async_context import (
    AsyncModelOverrideMiddleware,
    NovaAsyncContext,
)
from novacode_cli.config.model_create import build_async_agent_model


@tool
def get_changed_files(base_ref: str = "HEAD~1") -> str:
    """List files changed between the base ref and HEAD.

    Args:
        base_ref: Git reference to compare against (default HEAD~1).

    Returns:
        List of changed files with status (M/A/D).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", base_ref],
            capture_output=True,
            text=True,
            cwd=workspace_root(),
            timeout=30,
        )
        return (
            result.stdout or "No changes found."
            if result.returncode == 0
            else f"Error: {result.stderr}"
        )
    except Exception as e:
        return f"Error: {e}"


@tool
def get_diff_for_file(file_path: str, base_ref: str = "HEAD~1") -> str:
    """Get the diff for a specific file between base ref and HEAD.

    Args:
        file_path: Path to the file relative to project root.
        base_ref: Git reference to compare against.

    Returns:
        Unified diff output for the file.
    """
    try:
        result = subprocess.run(
            ["git", "diff", base_ref, "--", file_path],
            capture_output=True,
            text=True,
            cwd=workspace_root(),
            timeout=30,
        )
        return (
            result.stdout or "No diff."
            if result.returncode == 0
            else f"Error: {result.stderr}"
        )
    except Exception as e:
        return f"Error: {e}"


SYSTEM_PROMPT = """You are a Code Review Agent that runs asynchronously in the background.

Your purpose is to review code changes and provide actionable feedback:

1. **Analyze diffs** — Get changed files and their diffs
2. **Read full context** — Use `read_file` to read surrounding code when needed
3. **Check for issues** — Bugs, security vulnerabilities, performance problems, style violations
4. **Suggest improvements** — Concrete, actionable recommendations
5. **Rate the change** — Overall quality assessment

## Guidelines

- Be specific — reference exact line numbers and code snippets
- Prioritize: security issues > correctness > performance > style
- Don't nitpick — focus on meaningful improvements
- Acknowledge good patterns too, not just problems
- Format findings as a structured report with severity levels
"""


def _resolve_model() -> Any:
    return build_async_agent_model()


def _build_agent() -> Any:
    tools = [get_changed_files, get_diff_for_file]

    backend = FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True)

    return create_deep_agent(
        name="code-review-agent",
        model=_resolve_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        # Let a dispatch name the model to run on (see novacode_cli.agents.async_context).
        # The environment still decides when a run arrives without a context.
        middleware=[AsyncModelOverrideMiddleware()],
        context_schema=NovaAsyncContext,
    )


graph = _build_agent()

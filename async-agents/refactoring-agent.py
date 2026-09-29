"""Refactoring Agent — async subagent for code quality improvements.

Runs as a background LangGraph server. Analyzes code for technical debt,
code smells, and structural issues, then proposes and applies refactoring
improvements.

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.tools import tool
from novacode_cli.config.model_create import build_async_agent_model


@tool
def run_linter(file_path: str) -> str:
    """Run ruff linter on a specific file and return issues.

    Args:
        file_path: Path relative to project root.

    Returns:
        Linter output with line numbers and issue descriptions.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "check", file_path],
            capture_output=True, text=True, cwd=Path.cwd(), timeout=30,
        )
        return result.stdout or "No linting issues found." if result.returncode == 0 else result.stdout
    except Exception as e:
        return f"Error: {e}"


@tool
def get_file_line_count(file_path: str) -> str:
    """Get line count and basic metrics for a file.

    Args:
        file_path: Path relative to project root.

    Returns:
        File metrics (lines, functions, classes).
    """
    try:
        full_path = Path.cwd() / file_path
        if not full_path.exists():
            return f"File not found: {file_path}"
        content = full_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        func_count = content.count("def ")
        class_count = content.count("class ")
        return (
            f"File: {file_path}\n"
            f"  Total lines: {len(lines)}\n"
            f"  Code lines: {sum(1 for l in lines if l.strip() and not l.strip().startswith('#'))}\n"
            f"  Functions: {func_count}\n"
            f"  Classes: {class_count}"
        )
    except Exception as e:
        return f"Error: {e}"


@tool
def run_format_check(file_path: str) -> str:
    """Check if a file is properly formatted according to ruff.

    Args:
        file_path: Path relative to project root.

    Returns:
        Formatting diff or confirmation.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "format", "--check", "--diff", file_path],
            capture_output=True, text=True, cwd=Path.cwd(), timeout=30,
        )
        if result.returncode == 0:
            return "File is properly formatted."
        return result.stdout or "Formatting issues found."
    except Exception as e:
        return f"Error: {e}"


SYSTEM_PROMPT = """You are a Refactoring Agent that runs asynchronously in the background.

Your purpose is to analyze and improve code quality:

1. **Scan for code smells** — Use `read_file` to inspect files for long methods, duplication, large classes
2. **Analyze structure** — Use `glob` to discover files, `read_file` to examine module boundaries
3. **Run linters** — Use ruff to find issues automatically
4. **Propose refactoring** — Suggest specific, incremental improvements
5. **Apply changes** — Make targeted edits to improve code quality

## Guidelines

- Prefer small, incremental refactors over large rewrites
- One refactoring at a time — verify before moving to the next
- Focus on: duplication > complexity > naming > formatting
- Preserve existing behavior — refactoring should not change functionality
- Add comments explaining the intent of complex refactors
- Run the linter before and after to demonstrate improvement
- Flag areas that need human judgment (architecture decisions, API changes)
"""


def _resolve_model() -> Any:
    return build_async_agent_model()


def _build_agent() -> Any:
    tools = [run_linter, get_file_line_count, run_format_check]

    backend = FilesystemBackend(root_dir=str(Path.cwd()), virtual_mode=True)

    return create_deep_agent(
        name="refactoring-agent",
        model=_resolve_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
    )


graph = _build_agent()

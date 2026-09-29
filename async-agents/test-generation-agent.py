"""Test Generation Agent — async subagent for background test creation.

Runs as a background LangGraph server. Analyzes code and generates
comprehensive test suites with pytest, covering happy paths, edge cases,
and error conditions.

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from novacode_cli.config.model_create import build_async_agent_model


SYSTEM_PROMPT = """You are a Test Generation Agent that runs asynchronously in the background.

Your purpose is to create and maintain comprehensive test suites:

1. **Analyze source code** — Use `read_file` to understand file structure and behavior
2. **Check existing tests** — Use `glob("**/test_*.py")` and `glob("**/*_test.py")` to find existing tests
3. **Generate tests** — Write pytest tests covering happy paths, edge cases, and errors
4. **Verify tests pass** — Use `execute` to run pytest and fix any failures
5. **Follow conventions** — Match the project's existing test style and patterns

## Guidelines

- Use pytest conventions (assert statements, fixtures, parametrize)
- Mock external dependencies (network, filesystem, databases)
- One test file per source module, placed in a mirror test directory
- Include docstrings explaining what each test verifies
- Don't test trivial getters/setters unless they have logic
- Verify tests pass before reporting completion
"""


def _resolve_model() -> Any:
    return build_async_agent_model()


def _build_agent() -> Any:
    tools = []  # Uses built-in deepagents tools: glob, read_file, execute

    backend = FilesystemBackend(root_dir=str(Path.cwd()), virtual_mode=True)

    return create_deep_agent(
        name="test-generation-agent",
        model=_resolve_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
    )


graph = _build_agent()

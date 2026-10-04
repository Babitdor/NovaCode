"""Dependency Audit Agent — async subagent for dependency analysis.

Runs as a background LangGraph server. Audits project dependencies for
outdated packages, security vulnerabilities, license issues, and
suggests safe upgrade paths.

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

import json
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
def check_outdated_packages() -> str:
    """Check for outdated Python packages using pip.

    Returns:
        List of outdated packages with current and latest versions.
    """
    try:
        result = subprocess.run(
            ["uv", "pip", "list", "--outdated", "--format=json"],
            capture_output=True, text=True, cwd=workspace_root(), timeout=60,
        )
        if result.returncode != 0:
            return f"Error: {result.stderr}"
        packages = json.loads(result.stdout)
        if not packages:
            return "All packages are up to date."
        lines = ["Outdated packages:", ""]
        for pkg in packages:
            lines.append(f"  {pkg['name']}: {pkg['version']} → {pkg['latest_version']}")
        return "\n".join(lines)
    except json.JSONDecodeError:
        return "Could not parse package list."
    except FileNotFoundError:
        return "uv is not available."
    except Exception as e:
        return f"Error: {e}"


@tool
def check_security_advisories() -> str:
    """Check for known security vulnerabilities in dependencies.

    Uses pip-audit if available, otherwise falls back to safety or pip.

    Returns:
        Security advisory report.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "pip-audit", "--format=markdown"],
            capture_output=True, text=True, cwd=workspace_root(), timeout=120,
        )
        if result.returncode == 0:
            return result.stdout or "No known vulnerabilities found."
        return result.stdout or result.stderr or "pip-audit check completed with warnings."
    except FileNotFoundError:
        return "pip-audit is not installed. Install with: uv pip install pip-audit"
    except subprocess.TimeoutExpired:
        return "Security audit timed out."
    except Exception as e:
        return f"Error: {e}"


@tool
def get_dependency_tree() -> str:
    """Show the full dependency tree.

    Returns:
        Dependency tree output.
    """
    try:
        result = subprocess.run(
            ["uv", "tree"],
            capture_output=True, text=True, cwd=workspace_root(), timeout=60,
        )
        return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"
    except FileNotFoundError:
        return "uv is not available."
    except Exception as e:
        return f"Error: {e}"


SYSTEM_PROMPT = """You are a Dependency Audit Agent that runs asynchronously in the background.

Your purpose is to analyze and report on project dependencies:

1. **Read dependency config** — Use `read_file("pyproject.toml")` and `glob("requirements*.txt")` to check config
2. **Check for updates** — Find outdated packages and their latest versions
3. **Security audit** — Scan for known vulnerabilities (CVEs)
4. **Analyze dependency tree** — Understand transitive dependencies
5. **Suggest upgrades** — Recommend safe, compatible upgrade paths

## Guidelines

- Prioritize security vulnerabilities above all else
- Note breaking changes in major version bumps
- Suggest specific upgrade commands (e.g. `uv add package@latest`)
- Flag deprecated or unmaintained packages
- Report license issues if detectable
- Format as a structured report with severity: CRITICAL > HIGH > MEDIUM > LOW > INFO
"""


def _resolve_model() -> Any:
    return build_async_agent_model()


def _build_agent() -> Any:
    tools = [check_outdated_packages, check_security_advisories, get_dependency_tree]

    backend = FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True)

    return create_deep_agent(
        name="dependency-audit-agent",
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

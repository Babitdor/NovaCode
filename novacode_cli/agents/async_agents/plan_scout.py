"""Plan Scout Agent — async subagent that scans the directory for planning-relevant files.

Runs as a background LangGraph server. Dispatched by the plan agent to
parallelize codebase investigation before plan synthesis. Read-only: it
inspects files and returns a structured findings report, never modifies
anything.

Exports:
    graph: Compiled ``StateGraph`` instance for LangGraph Platform deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.tools import tool

from novacode_cli.agents.async_context import (
    AsyncModelOverrideMiddleware,
    NovaAsyncContext,
)
from novacode_cli.agents.async_workspace import workspace_root
from novacode_cli.config.model_create import build_async_agent_model
from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
from novacode_cli.skills.libraries import background_library


@tool
def scan_directory(directory: str = ".", max_depth: int = 4) -> str:
    """List the directory tree relevant to planning, up to a depth.

    Args:
        directory: Path relative to project root (default ".").
        max_depth: How deep to recurse (default 4).

    Returns:
        A tree of files and directories, skipping common vendored/ignored
        dirs (.git, node_modules, __pycache__, .venv, dist, build, .nova).
    """
    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
        "build", ".nova", ".pytest_cache", ".ruff_cache", ".mypy_cache",
        ".venv2", "site-packages", ".tox", ".eggs", "target", "coverage",
    }
    root = workspace_root() / directory
    root = root.resolve()
    if not root.exists():
        return f"Path not found: {directory}"

    lines: list[str] = []

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if entry.is_dir() and entry.name in SKIP_DIRS:
                continue
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                lines.append(f"{rel}/")
                walk(entry, depth + 1)
            else:
                lines.append(rel)

    walk(root, 0)
    if not lines:
        return f"No files under {directory}"
    return "\n".join(lines)


@tool
def summarize_file(file_path: str, max_lines: int = 80) -> str:
    """Return the first lines of a file as a planning preview.

    Args:
        file_path: Path relative to project root.
        max_lines: How many leading lines to return (default 80).

    Returns:
        The file's head, with a one-line header noting total length.
    """
    full_path = (workspace_root() / file_path).resolve()
    if not full_path.exists():
        return f"File not found: {file_path}"
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading {file_path}: {e}"
    lines = content.splitlines()
    head = lines[:max_lines]
    total = len(lines)
    preview = "\n".join(head)
    return f"--- {file_path} ({total} lines, showing first {min(max_lines, total)}) ---\n{preview}"


@tool
def search_references(pattern: str, path: str = ".", extensions: str = "py,ts,tsx,js,md") -> str:
    """Search files for a literal pattern (grep-style), returning matches.

    Args:
        pattern: The literal text to search for.
        path: Directory to search, relative to project root (default ".").
        extensions: Comma-separated file extensions to include.

    Returns:
        Matching lines with file:line prefixes, or "No matches found."
    """
    EXTS = {f".{e.strip().lstrip('.')}" for e in extensions.split(",") if e.strip()}
    root = (workspace_root() / path).resolve()
    if not root.exists():
        return f"Path not found: {path}"
    hits: list[str] = []
    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in EXTS:
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".nova"}
               for part in file.parts):
            continue
        try:
            for lineno, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern in line:
                    rel = file.relative_to(workspace_root()).as_posix()
                    hits.append(f"{rel}:{lineno}:{line.strip()[:200]}")
                    if len(hits) >= 200:
                        hits.append("... (truncated at 200 matches)")
                        return "\n".join(hits)
        except OSError:
            continue
    return "\n".join(hits) if hits else "No matches found."


SYSTEM_PROMPT = """You are a Plan Scout Agent that runs asynchronously in the background.

Your purpose is to scan a codebase directory and report findings back to a
planning agent, which synthesizes them into an implementation plan.

## Workflow

1. **Scan** — Call `scan_directory` to map the relevant tree (respect the
   requested depth; the plan agent names the area to explore).
2. **Summarize** — For the key files that look relevant to the planning
   question, call `summarize_file` to inspect their contents (headers,
   exports, signatures, wiring).
3. **Search** — If the planning question involves a specific symbol or
   feature, call `search_references` to locate usages and call chains.
4. **Report** — Return a concise, structured findings report (see format).

## Constraints

- **Read-only.** You may only inspect. Never edit, create, or delete files.
- **Be targeted.** Do not dump entire files. Summarize what is relevant and
  cite exact file paths so the planner can re-read them.
- **Answer the scouting question.** The plan agent handed you a specific
  area and question; your report must directly inform it.
- **Stay in scope.** Report facts and structure, not full implementation
  proposals. The plan agent does the synthesis.

## Report format

```
## Findings — <area / question>

### Relevant files
- `path/to/file.py` — role
- `path/to/config.json` — role

### Key facts
- What the code does today (with file:line evidence)
- Existing conventions / patterns that a plan must respect
- Risks or gotchas noticed (e.g. circular imports, test coupling)

### Gaps / unknowns
- What could not be determined from scanning (if any)
```

Keep the report under ~600 words. It is one of several parallel reports the
plan agent will merge, so be dense and factual.
"""


def _resolve_model() -> Any:
    return build_async_agent_model(per_agent_model_var="PLAN_SCOUT_MODEL")


def _build_agent() -> Any:
    tools = [scan_directory, summarize_file, search_references]

    backend = FilesystemBackend(root_dir=str(workspace_root()), virtual_mode=True)

    return create_deep_agent(
        name="plan-scout-agent",
        model=_resolve_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        # A dispatch may name the model to run on (see novacode_cli.agents.async_context).
        # PLAN_SCOUT_MODEL stays in charge when it is set, since it was aimed at this one
        # agent; otherwise the async role decides, and the environment is the last word.
        middleware=[AsyncModelOverrideMiddleware(per_agent_model_var="PLAN_SCOUT_MODEL"), DelegatedApprovalMiddleware(), background_library("plan-scout-agent")],
        context_schema=NovaAsyncContext,
    )


graph = _build_agent()

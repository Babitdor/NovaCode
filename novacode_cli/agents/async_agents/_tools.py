"""Tools the background agents name in their ``tools:`` frontmatter.

Moved here from the per-agent graph modules when those agents became markdown
files (``default_subagents/builtin/``). ``_specialist.build_specialist_graph``
resolves a declared tool name against ``novacode_cli.tools`` and then this module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain_core.tools import tool

# run_tests is named by test-runner-agent.md; it lives with its command allowlist.
from novacode_cli.agents.async_agents._test_commands import run_tests  # noqa: F401
from novacode_cli.agents.async_workspace import workspace_root


# from code_review.py
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


# from code_review.py
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


# from dependency_audit.py
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


# from dependency_audit.py
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


# from dependency_audit.py
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


# from documentation_update.py
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


# from documentation_update.py
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


# from documentation_update.py
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


# from refactoring.py
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
            capture_output=True, text=True, cwd=workspace_root(), timeout=30,
        )
        return result.stdout or "No linting issues found." if result.returncode == 0 else result.stdout
    except Exception as e:
        return f"Error: {e}"


# from refactoring.py
@tool
def get_file_line_count(file_path: str) -> str:
    """Get line count and basic metrics for a file.

    Args:
        file_path: Path relative to project root.

    Returns:
        File metrics (lines, functions, classes).
    """
    try:
        full_path = workspace_root() / file_path
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


# from refactoring.py
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
            capture_output=True, text=True, cwd=workspace_root(), timeout=30,
        )
        if result.returncode == 0:
            return "File is properly formatted."
        return result.stdout or "Formatting issues found."
    except Exception as e:
        return f"Error: {e}"


# from plan_scout.py
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


# from plan_scout.py
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


# from plan_scout.py
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

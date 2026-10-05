import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from novacode_cli.config.config import COLORS, Settings, console
from novacode_cli.config.model_create import create_model
from novacode_cli.skills.load import list_skills
from novacode_cli.skills.skill_system_prompt import render_skill_creation_prompt
from novacode_cli.tools import web_search


def _get_skill_query(
    skill_name: str,
    description: str | None = None,
) -> str:
    """Build a prompt for skill generation.

    Args:
        skill_name: Name of the skill.
        description: Optional user-provided description.
        research_context: Web search results for context.

    Returns:
        Complete prompt string for the LLM.
    """
    description_hint = ""
    if description:
        description_hint = f"""
The user has provided this description: "{description}"
Use this to guide the skill's purpose and content.
"""

    return f"""
    Create a comprehensive, production-ready SKILL.md file for a skill named "{skill_name}".

    {description_hint}
"""


async def _generate_skill(
    skill_name: str,
    base_dir: Path,
    description: str | None = None,
) -> str | None:
    """Generate skill content using the configured LLM and return SKILL.md content.

    This runs both from the standalone ``nova skills create`` CLI *and* from
    Hermes's autonomous skill creation (inside an agent turn). It must therefore
    never ``console.print`` — that would corrupt the Textual TUI. Progress/warnings
    go to the logger; callers surface their own user-facing outcome messages
    (the CLI prints "✓ created"; Hermes emits skill events).
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        logger.debug("Generating skill content for %s", skill_name)
        skill_dir = base_dir / skill_name

        skill_query_prompt = _get_skill_query(skill_name, description)

        # Use virtual_mode=True so the agent's file operations are scoped
        # to the skill directory (path traversal and absolute paths are blocked).
        # The system prompt must instruct the agent to use virtual paths like
        # "/SKILL.md" rather than absolute filesystem paths, matching how the
        # core agent works (see core_agent.py for the same pattern).
        # Lazy imports: deepagents + the filesystem backend are only needed to
        # build the skill-creation agent, not at module import time.
        from deepagents.graph import create_deep_agent
        from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend

        skill_creation_agent = create_deep_agent(
            name="Skill-Creation-Agent",
            model=create_model(),
            system_prompt=render_skill_creation_prompt(),
            tools=[web_search],
            backend=FilesystemBackend(root_dir=skill_dir, virtual_mode=True),
        )

        # Invoke the agent (this writes SKILL.md to disk)
        response = await skill_creation_agent.ainvoke(
            {"messages": [{"role": "user", "content": skill_query_prompt}]}
        )

        # Collect the full text response for fallback extraction
        responded = str(response["messages"][-1].content).strip()
        logger.debug("Skill agent responded with %d chars", len(responded))

        # --- Strategy 1: check if the agent wrote SKILL.md to disk ---
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            skill_content = skill_file.read_text(encoding="utf-8").strip()

            # Validate frontmatter
            if not skill_content.startswith("---"):
                logger.warning("SKILL.md missing frontmatter; adding defaults")
                skill_content = _add_frontmatter(skill_content, skill_name, description)
                skill_file.write_text(skill_content, encoding="utf-8")

            logger.debug("Skill content generated and normalized")
            return skill_content

        # --- Strategy 2: extract SKILL.md content from agent's text response ---
        if responded:
            extracted = _extract_skill_md_from_response(responded, skill_name)
            if extracted:
                logger.debug("Extracted SKILL.md from agent response (file not written)")
                skill_file.parent.mkdir(parents=True, exist_ok=True)
                skill_file.write_text(extracted, encoding="utf-8")
                return extracted

            logger.debug(
                "Agent responded (%d chars) but SKILL.md was not written and "
                "no markdown content could be extracted from the response.",
                len(responded),
            )

        logger.warning("SKILL.md was not created by the agent")
        return None

    except Exception as e:
        logger.debug("Skill generation exception", exc_info=True)
        logger.warning("LLM skill generation failed (%s); using static template", e)
        return None


def _add_frontmatter(content: str, name: str, description: str | None) -> str:
    """Ensure YAML frontmatter with name and description."""
    return f"---\nname: {name}\ndescription: {description or 'No description provided'}\n---\n\n{content}"


def _extract_skill_md_from_response(response: str, skill_name: str) -> str | None:
    """Try to extract SKILL.md content from the agent's text response.

    Some models return markdown content inline instead of writing it via
    write_file.  This function tries to pull out the SKILL.md payload.

    Strategies (in order):
    1. Find a ```markdown ... ``` fenced block
    2. Find content between YAML frontmatter delimiters (--- ... ---)
    3. Use the entire response if it looks like valid markdown

    Returns:
        Extracted SKILL.md content with frontmatter, or None.
    """
    import re

    # Strategy 1: Extract from fenced code block
    # Match ```markdown, ```md, or ``` (with optional language tag)
    fence_pattern = re.compile(
        r"```(?:markdown|md|yaml)?\s*\n(.*?)```",
        re.DOTALL,
    )
    matches = fence_pattern.findall(response)
    for match in matches:
        stripped = match.strip()
        if stripped.startswith("---") or "# " in stripped:
            return _ensure_frontmatter(stripped, skill_name)

    # Strategy 2: Find YAML frontmatter block in the response
    fm_pattern = re.compile(
        r"(---\s*\n.*?\n---\s*\n.*?)(?=\n---\s*\n|$)",
        re.DOTALL,
    )
    fm_matches = fm_pattern.findall(response)
    for match in fm_matches:
        stripped = match.strip()
        if stripped.startswith("---") and len(stripped) > 50:
            return stripped

    # Strategy 3: Check if the response itself looks like SKILL.md content
    stripped_response = response.strip()
    if stripped_response.startswith("---") or stripped_response.startswith("# "):
        return _ensure_frontmatter(stripped_response, skill_name)

    return None


def _ensure_frontmatter(content: str, skill_name: str) -> str:
    """Ensure content has valid YAML frontmatter."""
    if content.startswith("---"):
        return content
    return f"---\nname: {skill_name}\ndescription: Auto-generated skill\n---\n\n{content}"


def _get_static_template(skill_name: str) -> str:
    """Get the static template for skill creation (fallback).

    Args:
        skill_name: Name of the skill.

    Returns:
        Static SKILL.md template content.
    """
    skill_title = skill_name.replace("-", " ").replace("_", " ").title()
    return f"""---
name: {skill_name}
description: <verb> <object>. Use when: <signal 1>, <signal 2>.
version: 1.0.0
tags: []
---

# {skill_title}

## When to Use
- [The concrete situation/trigger that should reach for this skill]

## Quick Reference
```bash
# The key command(s), copy-pasteable
example-command --flag value
```

## Procedure
1. [First step — concrete, reference real files/commands]
2. [Second step]
3. [Third step]

## Pitfalls
- [Edge case or mistake to avoid, and how]

## Verification
- [How to confirm the work is correct — the check/test to run]
"""


def _create(
    skill_name: str,
    agent: str,
    project: bool = False,
    global_scope: bool = False,
    ask: bool = True,
) -> None:
    """Create a new skill with a template SKILL.md file.

    Args:
        skill_name: Name of the skill to create.
        agent: Agent identifier for skills
        project: If True, create in project skills directory.
        global_scope: If True, create in global skills directory.
        ask: If True and neither project nor global_scope is specified, prompt user interactively.
    """
    # Validate skill name first
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid skill name: {error_msg}")
        console.print(
            "[dim]Skill names must only contain letters, numbers, hyphens, and underscores.[/dim]",
            style=COLORS["dim"],
        )
        return

    # Determine scope - either from flags or by asking
    if project and global_scope:
        console.print(
            "[bold red]Error:[/bold red] Cannot specify both --project and --global flags."
        )
        return

    use_project = project
    if not project and not global_scope and ask:
        # Ask user interactively
        scope = _ask_scope("create")
        if scope is None:
            console.print("Cancelled.", style=COLORS["dim"])
            return
        use_project = scope == "project"
    # If global_scope is True, use_project remains False

    # Determine target directory
    settings = Settings.from_environment()
    if use_project:
        if not settings.project_root:
            console.print("[bold red]Error:[/bold red] Not in a project directory.")
            console.print(
                "[dim]Project skills require a .git directory in the project root.[/dim]",
                style=COLORS["dim"],
            )
            return
        skills_dir = settings.ensure_project_skills_dir()
    else:
        skills_dir = settings.ensure_user_skills_dir(agent)

    skill_dir = skills_dir / skill_name  # type: ignore

    # Validate the resolved path is within skills_dir
    is_valid_path, path_error = _validate_skill_path(skill_dir, skills_dir)  # type: ignore
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        return

    if skill_dir.exists():
        console.print(
            f"[bold red]Error:[/bold red] Skill '{skill_name}' already exists at {skill_dir}"
        )
        return

    # Create skill directory
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Try to generate content with LLM, fall back to static template.
    # _generate_skill is async; use asyncio.run() since _create is sync.
    # Pass skills_dir (the parent) not skill_dir — _generate_skill appends
    # skill_name internally to build the correct path.
    import asyncio
    content = asyncio.run(_generate_skill(skill_name, skills_dir)) # type: ignore
    if content is None:
        content = _get_static_template(skill_name)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")
        used_llm = False
    else:
        # _generate_skill already wrote SKILL.md via the agent; no second write needed.
        used_llm = True
    skill_md = skill_dir / "SKILL.md"

    console.print(f"✓ Skill '{skill_name}' created successfully!", style=COLORS["primary"])
    console.print(f"Location: {skill_dir}\n", style=COLORS["dim"])

    if used_llm:
        files_created = ["SKILL.md"]
        console.print(
            f"[dim]Files created: {', '.join(files_created)}\n"
            "\n"
            "The skill was generated using AI. Review and customize as needed:\n"
            f"  nano {skill_md}\n",
            style=COLORS["dim"],
        )

    else:
        console.print(
            "[dim]Edit the SKILL.md file to customize:\n"
            "  1. Update the description in YAML frontmatter\n"
            "  2. Fill in the instructions and examples\n"
            "  3. Add any supporting files (scripts, configs, etc.)\n"
            "\n"
            f"  nano {skill_md}\n",
            style=COLORS["dim"],
        )


def _validate_skill_path(skill_dir: Path, base_dir: Path) -> tuple[bool, str]:
    """Validate that the resolved skill directory is within the base directory.

    Args:
        skill_dir: The skill directory path to validate
        base_dir: The base skills directory that should contain skill_dir

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    try:
        # Resolve both paths to their canonical form
        resolved_skill = skill_dir.resolve()
        resolved_base = base_dir.resolve()

        # Check if skill_dir is within base_dir
        # Use is_relative_to if available (Python 3.9+), otherwise use string comparison
        if hasattr(resolved_skill, "is_relative_to"):
            if not resolved_skill.is_relative_to(resolved_base):
                return False, f"Skill directory must be within {base_dir}"
        else:
            # Fallback for older Python versions
            try:
                resolved_skill.relative_to(resolved_base)
            except ValueError:
                return False, f"Skill directory must be within {base_dir}"

        return True, ""
    except (OSError, RuntimeError) as e:
        return False, f"Invalid path: {e}"


def _ask_scope(operation: str = "use", allow_both: bool = False) -> str | None:
    """Ask user whether to use project or global scope.

    Args:
        operation: The operation being performed (e.g., "create", "use", "list")
        allow_both: If True, add a "both" option (for list/info commands)

    Returns:
        "project", "global", or "both" (if allow_both=True), or None if user cancels
    """
    # Check if we're in a project directory
    settings = Settings.from_environment()
    in_project = settings.project_root is not None

    console.print(f"\nWhere do you want to {operation} skills?", style=COLORS["primary"])

    if in_project:
        console.print("  1. Project-specific (current project only)")
        console.print("  2. Global (all projects)")
        if allow_both:
            console.print("  3. Both (project and global)")
        console.print()

        max_choice = "3" if allow_both else "2"
        default_choice = "3" if allow_both else "1"
        choice = input(f"Choose (1-{max_choice}) [{default_choice}]: ").strip() or default_choice

        if choice == "1":
            return "project"
        if choice == "2":
            return "global"
        if choice == "3" and allow_both:
            return "both"
        return "project" if not allow_both else "both"
    console.print("[yellow]Not in a project directory. Using global skills.[/yellow]")
    console.print(
        "[dim]Project skills require a .git directory in the project root.[/dim]",
        style=COLORS["dim"],
    )
    return "global"


def _validate_name(name: str) -> tuple[bool, str]:
    """Validate name to prevent path traversal attacks.

    Args:
        name: The name to validate

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    # Check for empty or whitespace-only names
    if not name or not name.strip():
        return False, "cannot be empty"

    # Check for path traversal sequences
    if ".." in name:
        return False, "name cannot contain '..' (path traversal)"

    # Check for absolute paths
    if name.startswith(("/", "\\")):
        return False, "name cannot be an absolute path"

    # Check for path separators
    if "/" in name or "\\" in name:
        return False, "name cannot contain path separators"

    # Only allow alphanumeric, hyphens, underscores
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return False, "name can only contain letters, numbers, hyphens, and underscores"

    return True, ""


def _list(
    agent: str, *, project: bool = False, global_scope: bool = False, ask: bool = True
) -> None:
    """List all available skills for the specified agent.

    Args:
        agent: Agent identifier for skills (default: agent).
        project: If True, show only project skills.
        global_scope: If True, show only global skills.
        ask: If True and no flags specified, prompt user interactively.
    """
    settings = Settings.from_environment()
    user_skills_dir = settings.get_user_skills_dir(agent)
    project_skills_dir = settings.get_project_skills_dir() or Path.cwd() / ".nova" / "skills"
    project_skills_dirs = settings.get_project_skills_dirs()

    # Determine what to show - from flags or by asking
    if project and global_scope:
        console.print(
            "[bold red]Error:[/bold red] Cannot specify both --project and --global flags."
        )
        return

    show_scope = "both"  # Default
    if project:
        show_scope = "project"
    elif global_scope:
        show_scope = "global"
    elif ask:
        # Ask user interactively
        scope = _ask_scope("list", allow_both=True)
        if scope is None:
            console.print("Cancelled.", style=COLORS["dim"])
            return
        show_scope = scope

    # Handle project-only view
    if show_scope == "project":
        skills = list_skills(
            project_skills_dir=project_skills_dir,
            project_skills_dirs=project_skills_dirs,
        )
        console.print("\n[bold]Project Skills:[/bold]\n", style=COLORS["primary"])
    elif show_scope == "global":
        # Load only global (nova + claude) skills
        claude_skills_dir = Settings.get_global_claude_skills_dir()
        skills = list_skills(
            user_skills_dir=user_skills_dir,
            shared_skills_dir=Settings.get_shared_skills_dir(),
            claude_skills_dir=claude_skills_dir if claude_skills_dir.exists() else None,
            project_skills_dir=None,
        )
        console.print("\n[bold]Global Skills:[/bold]\n", style=COLORS["primary"])
    else:
        # Load all skills (nova + claude + project)
        claude_skills_dir = Settings.get_global_claude_skills_dir()
        skills = list_skills(
            user_skills_dir=user_skills_dir,
            shared_skills_dir=Settings.get_shared_skills_dir(),
            claude_skills_dir=claude_skills_dir if claude_skills_dir.exists() else None,
            project_skills_dir=project_skills_dir,
            project_skills_dirs=project_skills_dirs,
        )

        if not skills:
            console.print("[yellow]No skills found.[/yellow]")
            console.print(
                "[dim]Skills will be created in ~/.nova/agent/skills/ when you add them.[/dim]",
                style=COLORS["dim"],
            )
            console.print(
                "\n[dim]Create your first skill:\n  Nova skills create my-skill[/dim]",
                style=COLORS["dim"],
            )
            return

        console.print("\n[bold]Available Skills:[/bold]\n", style=COLORS["primary"])

    # Check if we have any skills
    if not skills:
        if show_scope == "global":
            console.print("[yellow]No global skills found.[/yellow]")
            console.print(
                "[dim]Global skills will be created in ~/.nova/skills/ when you add them.[/dim]",
                style=COLORS["dim"],
            )
            console.print(
                "\n[dim]Create a global skill:\n  Nova skills create my-skill --global[/dim]",
                style=COLORS["dim"],
            )
        # Project and both cases are handled above
        return

    # Group skills by source
    user_skills = [s for s in skills if s["source"] == "user"]
    project_skills_list = [s for s in skills if s["source"] == "project"]

    # Show user skills (for global-only or both views)
    if user_skills and show_scope in ["global", "both"]:
        console.print("[bold cyan]User Skills:[/bold cyan]", style=COLORS["primary"])
        for skill in user_skills:
            skill_path = Path(skill["path"])
            console.print(f"  • [bold]{skill['name']}[/bold]", style=COLORS["primary"])
            console.print(f"    {skill['description']}", style=COLORS["dim"])
            console.print(f"    Location: {skill_path.parent}/", style=COLORS["dim"])
            console.print()

    # Show project skills (for project-only or both views)
    if project_skills_list and show_scope in ["project", "both"]:
        if show_scope == "both" and user_skills:
            console.print()
        console.print("[bold green]Project Skills:[/bold green]", style=COLORS["primary"])
        for skill in project_skills_list:
            skill_path = Path(skill["path"])
            console.print(f"  • [bold]{skill['name']}[/bold]", style=COLORS["primary"])
            console.print(f"    {skill['description']}", style=COLORS["dim"])
            console.print(f"    Location: {skill_path.parent}/", style=COLORS["dim"])
            console.print()


def _info(
    skill_name: str,
    *,
    agent: str = "agent",
    project: bool = False,
    global_scope: bool = False,
    ask: bool = True,
) -> None:
    """Show detailed information about a specific skill.

    Args:
        skill_name: Name of the skill to show info for.
        agent: Agent identifier for skills (default: agent).
        project: If True, only search in project skills.
        global_scope: If True, only search in global skills.
        ask: If True and no flags specified, prompt user interactively.
    """
    settings = Settings.from_environment()
    user_skills_dir = settings.get_user_skills_dir(agent)
    project_skills_dir = settings.get_project_skills_dir() or Path.cwd() / ".nova" / "skills"
    project_skills_dirs = settings.get_project_skills_dirs()

    # Determine what to search - from flags or by asking
    if project and global_scope:
        console.print(
            "[bold red]Error:[/bold red] Cannot specify both --project and --global flags."
        )
        return

    search_scope = "both"  # Default
    if project:
        search_scope = "project"
    elif global_scope:
        search_scope = "global"
    elif ask:
        # Ask user interactively
        scope = _ask_scope("search", allow_both=True)
        if scope is None:
            console.print("Cancelled.", style=COLORS["dim"])
            return
        search_scope = scope

    # Load skills based on scope
    if search_scope == "project":
        skills = list_skills(
            project_skills_dir=project_skills_dir,
            project_skills_dirs=project_skills_dirs,
        )
    elif search_scope == "global":
        claude_skills_dir = Settings.get_global_claude_skills_dir()
        skills = list_skills(
            user_skills_dir=user_skills_dir,
            shared_skills_dir=Settings.get_shared_skills_dir(),
            claude_skills_dir=claude_skills_dir if claude_skills_dir.exists() else None,
            project_skills_dir=None,
        )
    else:
        claude_skills_dir = Settings.get_global_claude_skills_dir()
        skills = list_skills(
            user_skills_dir=user_skills_dir,
            shared_skills_dir=Settings.get_shared_skills_dir(),
            claude_skills_dir=claude_skills_dir if claude_skills_dir.exists() else None,
            project_skills_dir=project_skills_dir,
            project_skills_dirs=project_skills_dirs,
        )

    # Find the skill
    skill = next((s for s in skills if s["name"] == skill_name), None)

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' not found.")
        console.print("\n[dim]Available skills:[/dim]", style=COLORS["dim"])
        for s in skills:
            console.print(f"  - {s['name']}", style=COLORS["dim"])
        return

    # Read the full SKILL.md file
    skill_path = Path(skill["path"])
    skill_content = skill_path.read_text()

    # Determine source label
    source_label = "Project Skill" if skill["source"] == "project" else "User Skill"
    source_color = "green" if skill["source"] == "project" else "cyan"

    console.print(
        f"\n[bold]Skill: {skill['name']}[/bold] [bold {source_color}]({source_label})[/bold {source_color}]\n",
        style=COLORS["primary"],
    )
    console.print(f"[bold]Description:[/bold] {skill['description']}\n", style=COLORS["dim"])
    console.print(f"[bold]Location:[/bold] {skill_path.parent}/\n", style=COLORS["dim"])

    # List supporting files
    skill_dir = skill_path.parent
    supporting_files = [f for f in skill_dir.iterdir() if f.name != "SKILL.md"]

    if supporting_files:
        console.print("[bold]Supporting Files:[/bold]", style=COLORS["dim"])
        for file in supporting_files:
            console.print(f"  - {file.name}", style=COLORS["dim"])
        console.print()

    # Show the full SKILL.md content
    console.print("[bold]Full SKILL.md Content:[/bold]\n", style=COLORS["primary"])
    console.print(skill_content, style=COLORS["dim"])
    console.print()


def _get_skills_dir(
    agent: str,
    project: bool = False,
    global_scope: bool = False,
    ask: bool = True,
) -> tuple[Path | None, bool]:
    """Get the skills directory based on scope settings.

    Args:
        agent: Agent identifier for skills.
        project: If True, return project skills directory.
        global_scope: If True, return global skills directory.
        ask: If True and neither flag specified, prompt user interactively.

    Returns:
        Tuple of (skills_dir, is_project). is_project indicates if scope is project.
    """
    settings = Settings.from_environment()

    # Validate flags
    if project and global_scope:
        console.print(
            "[bold red]Error:[/bold red] Cannot specify both --project and --global flags."
        )
        return None, False

    use_project = project
    if not project and not global_scope and ask:
        scope = _ask_scope("install")
        if scope is None:
            console.print("Cancelled.", style=COLORS["dim"])
            return None, False
        use_project = scope == "project"

    if use_project:
        if not settings.project_root:
            console.print("[bold red]Error:[/bold red] Not in a project directory.")
            console.print(
                "[dim]Project skills require a .git directory in the project root.[/dim]",
                style=COLORS["dim"],
            )
            return None, False
        return settings.ensure_project_skills_dir(), True
    else:
        return settings.ensure_user_skills_dir(agent), False


def _skill_exists(skill_name: str, skills_dir: Path) -> Path | None:
    """Check if a skill already exists in the given directory.

    Args:
        skill_name: Name of the skill to check.
        skills_dir: Directory to search in.

    Returns:
        Path to existing skill directory if found, None otherwise.
    """
    skill_dir = skills_dir / skill_name
    return skill_dir if skill_dir.exists() else None


def _parse_github_url(url: str) -> tuple[str, str, str, str] | None:
    """Parse a GitHub URL to extract owner, repo, branch, and path.

    Supports URLs like:
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/branch
    - https://github.com/owner/repo/blob/branch/path/to/SKILL.md

    Args:
        url: GitHub URL to parse.

    Returns:
        Tuple of (owner, repo, branch, path) or None if invalid.
        path is the directory path within the repo (empty string if not specified).
    """
    import re

    # Handle github.com/owner/repo format
    github_pattern = r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)"
    match = re.search(github_pattern, url)
    if not match:
        return None

    owner, repo = match.groups()
    repo = repo.rstrip("/")

    # Remove .git suffix if present
    repo = re.sub(r"\.git$", "", repo)

    # Extract branch and path
    branch = "main"  # Default branch
    path = ""  # Default empty path

    # Check for /tree/ or /blob/ with branch and path
    path_match = re.search(r"/(?:tree|blob)/([^/]+)(?:/(.+))?$", url)
    if path_match:
        branch = path_match.group(1)
        path = path_match.group(2) or ""

    return owner, repo, branch, path


# Known non-content directories to skip when fetching skill files
# (hidden dirs starting with . are always skipped regardless)
_SKIP_DIRS = {".git", ".github", ".vscode", "__pycache__", "node_modules", ".venv", "dist", "build", ".gitlab"}
# File extensions to skip — binary or archive formats
_SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz", ".exe", ".bin"}
# Max size for supporting files (100 KB)
_MAX_SUPPORTING_FILE_SIZE = 100_000  # 100 KB
# Root-level metadata files that belong to the repo, not the skill content
_ROOT_SKIP_FILES = {"README.md", "LICENSE.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
                    "CHANGELOG.md", "SECURITY.md", ".gitignore"}


def _fetch_skill_from_github(
    owner: str,
    repo: str,
    branch: str = "main",
    path: str = "",
    preferred_name: str | None = None,
) -> tuple[str | None, str | None, dict[str, str]]:
    """Fetch SKILL.md and supporting files from a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch: Branch name (default: main).
        path: Subdirectory path within the repo (e.g., "skills/find-skills").
        preferred_name: When set (from --skill flag), try {preferred_name}/SKILL.md
            first so multi-skill repos like obra/superpowers serve the right skill.

    Returns:
        Tuple of (skill_content, skill_name, supporting_files) where supporting_files
        is a dict of {relative_path: file_content}. Returns (None, None, {}) if failed.
    """
    import requests

    api_base = "https://api.github.com"

    def _try_raw(skill_path: str) -> tuple[str, str] | None:
        """Fetch a single raw path; return (content, name) or None."""
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{skill_path}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                content = r.text.strip()
                name = _extract_skill_name(content)
                return content, name or repo
        except requests.RequestException:
            pass
        return None

    def _fetch_supporting_files(skill_md_path: str, tree_items: list[dict]) -> dict[str, str]:
        """Fetch supporting files from the same directory as SKILL.md.

        Fetches two categories of files:
        1. Files sitting *alongside* SKILL.md at the skill's root (e.g.
           LANGUAGE.md, HTML-REPORT.md, CONTEXT.md, etc.)
        2. Files inside known supporting subdirectories (scripts/, assets/,
           references/, examples/, templates/, prompts/, data/)

        Args:
            skill_md_path: Path to the found SKILL.md (e.g. "my-skill/SKILL.md")
            tree_items: Full list of tree items from the GitHub Trees API

        Returns:
            Dict of {path_relative_to_skill_dir: file_content}
        """
        # Determine the skill's directory in the repo
        if "/" in skill_md_path:
            skill_dir_prefix = skill_md_path.rsplit("/", 1)[0] + "/"
        else:
            # SKILL.md is at repo root — skill dir IS the root
            skill_dir_prefix = ""

        supporting: dict[str, str] = {}

        for item in tree_items:
            if item["type"] != "blob":
                continue
            item_path: str = item["path"]

            # Must be within the skill directory
            if skill_dir_prefix and not item_path.startswith(skill_dir_prefix):
                continue

            # Relative path from skill dir
            rel_path = item_path[len(skill_dir_prefix):]

            # Skip SKILL.md itself and hidden files
            if rel_path == "SKILL.md" or rel_path.startswith("."):
                continue

            # Determine whether to include this file:
            # - Hidden dirs (.github, .vscode, etc.): skip
            # - Known build/CI directories: skip
            # - Root-level metadata files (README.md, LICENSE.md): skip
            # - Everything else: include
            top_level = rel_path.split("/")[0]

            # Skip hidden directories
            if top_level.startswith("."):
                continue

            # Skip known non-content directories
            if top_level in _SKIP_DIRS:
                continue

            # Root-level path components
            if "/" not in rel_path and rel_path in _ROOT_SKIP_FILES:
                continue

            # Skip binary/large file extensions
            suffix = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
            if suffix in _SKIP_EXTENSIONS:
                continue

            # Skip blobs over size limit
            blob_size = item.get("size", 0) or 0
            if blob_size > _MAX_SUPPORTING_FILE_SIZE:
                continue

            # Fetch the file
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{item_path}"
            try:
                r = requests.get(raw_url, timeout=10)
                if r.status_code == 200:
                    supporting[rel_path] = r.text
            except requests.RequestException:
                pass

        return supporting

    # Build the base path for SKILL.md locations
    base_path = path.strip("/") if path else ""

    # We need the full repo tree for supporting-file scanning. Fetch it once.
    tree_items: list[dict] = []
    try:
        branch_url = f"{api_base}/repos/{owner}/{repo}/branches/{branch}"
        branch_resp = requests.get(branch_url, timeout=10)
        if branch_resp.status_code == 200:
            sha = branch_resp.json()["commit"]["sha"]
            tree_url = f"{api_base}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1"
            tree_resp = requests.get(tree_url, timeout=15)
            if tree_resp.status_code == 200:
                tree_items = tree_resp.json().get("tree", [])
    except requests.RequestException:
        pass

    def _try_with_support(skill_path: str) -> tuple[str, str, dict[str, str]] | None:
        """Fetch SKILL.md and its supporting files."""
        result = _try_raw(skill_path)
        if result:
            content, name = result
            supporting = _fetch_supporting_files(skill_path, tree_items) if tree_items else {}
            return content, name, supporting
        return None

    # --skill hint: try {preferred_name}/SKILL.md before anything else so the
    # user gets exactly the skill they asked for in a multi-skill repo.
    if preferred_name and not base_path:
        result = _try_with_support(f"{preferred_name}/SKILL.md")
        if result:
            return result

    # If a specific path was provided in the URL (e.g., pointing to a skill directory),
    # try that location first
    if base_path:
        target = base_path if base_path.endswith("/SKILL.md") else f"{base_path}/SKILL.md"
        result = _try_with_support(target)
        if result:
            return result

    # Try common SKILL.md locations at root level
    for skill_path in ("SKILL.md", "skills/SKILL.md", "skill/SKILL.md", ".skills/SKILL.md"):
        result = _try_with_support(skill_path)
        if result:
            return result

    # Full recursive scan via Git Trees API — tree_items already fetched above
    if tree_items:
        all_skill_paths = [
            item["path"] for item in tree_items
            if item["type"] == "blob" and item["path"].endswith("SKILL.md")
        ]

        # If a preferred name was given, try paths whose parent directory
        # matches it (e.g. "writing-plans/SKILL.md") before falling back
        # to shallowest-first ordering.
        if preferred_name:
            preferred = [
                p for p in all_skill_paths
                if p.split("/")[-2] == preferred_name
            ] if any("/" in p for p in all_skill_paths) else []
            ordered = preferred + [p for p in sorted(all_skill_paths, key=lambda p: p.count("/")) if p not in preferred]
        else:
            ordered = sorted(all_skill_paths, key=lambda p: p.count("/"))

        for skill_path in ordered:
            result = _try_with_support(skill_path)
            if result:
                return result

    return None, None, {}


def _extract_skill_name(content: str) -> str | None:
    """Extract skill name from SKILL.md frontmatter.

    Args:
        content: SKILL.md content.

    Returns:
        Skill name if found in frontmatter, None otherwise.
    """
    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 2:
        return None

    frontmatter = parts[1]

    # Look for name: field
    name_match = re.search(r"^\s*name:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    if name_match:
        return name_match.group(1).strip()

    return None


def _fetch_repo_context(owner: str, repo: str, branch: str = "main") -> tuple[str | None, str | None]:
    """Fetch README and description from a GitHub repo for LLM-based skill generation.

    Returns:
        Tuple of (readme_content, repo_description). Either may be None.
    """
    import requests

    readme_content: str | None = None
    repo_description: str | None = None

    # Fetch repo metadata (description)
    try:
        meta = requests.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=10)
        if meta.status_code == 200:
            repo_description = meta.json().get("description") or None
    except requests.RequestException:
        pass

    # Fetch README
    for readme_name in ("README.md", "readme.md", "Readme.md", "README.rst", "README"):
        try:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme_name}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                readme_content = r.text[:8000]  # Cap at 8 KB to stay within LLM context
                break
        except requests.RequestException:
            continue

    return readme_content, repo_description


def _add(
    url: str,
    agent: str,
    project: bool = False,
    global_scope: bool = False,
    force: bool = False,
    skill_name: str | None = None,
) -> None:
    """Add a skill from a GitHub URL.

    If the repository contains a SKILL.md file it is installed directly.
    Otherwise the repo's README and description are used to auto-generate
    a SKILL.md with the LLM.

    Args:
        url: GitHub URL to the skill repository.
        agent: Agent identifier for skills.
        project: If True, install in project skills directory.
        global_scope: If True, install in global skills directory.
        force: If True, overwrite existing skill.
        skill_name: Override the skill name (default: derived from repo or SKILL.md).
    """
    import asyncio

    # Parse GitHub URL
    parsed = _parse_github_url(url)
    if not parsed:
        console.print(f"[bold red]Error:[/bold red] Invalid GitHub URL: {url}")
        console.print(
            "\n[dim]Supported formats:\n"
            "  https://github.com/owner/repo\n"
            "  https://github.com/owner/repo/tree/branch\n"
            "  https://github.com/owner/repo/blob/branch/path/to/SKILL.md[/dim]",
            style=COLORS["dim"],
        )
        return

    owner, repo, branch, path = parsed

    # Validate --skill override early, before any network I/O
    if skill_name is not None:
        is_valid, error_msg = _validate_name(skill_name)
        if not is_valid:
            console.print(f"[bold red]Error:[/bold red] Invalid --skill name: {error_msg}")
            return

    console.print(f"[dim]Fetching skill from {owner}/{repo} (branch: {branch}, path: {path or 'root'})...[/dim]")

    # Try to fetch an existing SKILL.md first.
    # Pass skill_name as a hint so the fetcher tries {skill_name}/SKILL.md first
    # (useful for multi-skill repos like obra/superpowers).
    content, fetched_name, supporting_files = _fetch_skill_from_github(owner, repo, branch, path, preferred_name=skill_name)
    # --skill flag overrides whatever name was derived from the repo/SKILL.md
    if skill_name is None:
        skill_name = fetched_name
    generated = False

    if not content:
        # No SKILL.md — fall back to generating one from the repo's README
        console.print(
            f"[dim]No SKILL.md found in {owner}/{repo}. "
            "Fetching README to generate skill...[/dim]"
        )
        readme, repo_desc = _fetch_repo_context(owner, repo, branch)

        if not readme and not repo_desc:
            console.print(
                f"[bold red]Error:[/bold red] Could not retrieve any content from {owner}/{repo}.\n"
                "[dim]Make sure the repository is public and accessible.[/dim]"
            )
            return

        # Build a rich description for the LLM
        description_parts = []
        if repo_desc:
            description_parts.append(f"Repository description: {repo_desc}")
        if readme:
            description_parts.append(f"README:\n\n{readme}")
        description = "\n\n".join(description_parts)

        # Derive a skill name from the repo name (sanitise to allowed chars),
        # unless the caller already provided one via --skill
        if skill_name is None:
            raw_name = repo.lower().replace(" ", "-")
            skill_name = re.sub(r"[^a-z0-9_-]", "-", raw_name).strip("-")
            if not skill_name:
                skill_name = "imported-skill"

        console.print(f"[dim]Generating skill '{skill_name}' from repo content...[/dim]")

        # Get target directory early so we can pass skills_dir to the generator
        skills_dir, is_project = _get_skills_dir(agent, project, global_scope)
        if skills_dir is None:
            return

        skill_dir = skills_dir / skill_name
        is_valid_path, path_error = _validate_skill_path(skill_dir, skills_dir)
        if not is_valid_path:
            console.print(f"[bold red]Error:[/bold red] {path_error}")
            return

        existing = _skill_exists(skill_name, skills_dir)
        if existing and not force:
            console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' already exists.")
            console.print(f"  Location: {existing}")
            console.print("\n[dim]Use --force to overwrite.[/dim]", style=COLORS["dim"])
            return

        skill_dir.mkdir(parents=True, exist_ok=True)
        content = asyncio.run(_generate_skill(skill_name, skills_dir, description=description))
        if not content:
            # LLM failed — write a minimal stub so the install still succeeds
            content = _get_static_template(skill_name)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

        generated = True
        scope_label = "project" if is_project else "global"
        _print_skill_installed_banner(skill_name, str(skill_dir), scope_label, f"{owner}/{repo}")
        try:
            from novacode_cli.skills.skill_lock import SkillLock
            SkillLock.for_skills_dir(skills_dir).update(skill_name, {
                "source": url,
                "branch": branch,
                "skill_name_override": skill_name,
                "installed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "scope": "project" if is_project else "global",
                "generated": True,
            })
        except Exception:
            pass
        return

    console.print(f"[dim]Found skill: {skill_name}[/dim]")

    # Validate skill name
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid skill name from repository: {error_msg}")
        console.print(
            "[dim]The 'name' field in SKILL.md frontmatter contains invalid characters.[/dim]",
            style=COLORS["dim"],
        )
        return

    # Get target directory
    skills_dir, is_project = _get_skills_dir(agent, project, global_scope)
    if skills_dir is None:
        return  # User cancelled

    skill_dir = skills_dir / skill_name

    # Validate the resolved path is within skills_dir
    is_valid_path, path_error = _validate_skill_path(skill_dir, skills_dir)
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        return

    # Check for existing skill
    existing = _skill_exists(skill_name, skills_dir)
    if existing and not force:
        console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' already exists.")
        console.print(f"  Location: {existing}")
        console.print("\n[dim]Use --force to overwrite existing skill.[/dim]", style=COLORS["dim"])
        return

    # Create skill directory
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write SKILL.md
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")

    # Write supporting files (scripts/, assets/, examples/, etc.)
    if supporting_files:
        for rel_path, file_content in supporting_files.items():
            dest = skill_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(file_content, encoding="utf-8")
        console.print(f"[dim]Copied {len(supporting_files)} supporting file(s) → {skill_dir}[/dim]")

    scope_label = "project" if is_project else "global"
    _print_skill_installed_banner(skill_name, str(skill_dir), scope_label, f"{owner}/{repo}")
    try:
        from novacode_cli.skills.skill_lock import SkillLock
        SkillLock.for_skills_dir(skills_dir).update(skill_name, {
            "source": url,
            "branch": branch,
            "skill_name_override": skill_name if skill_name != fetched_name else None,
            "installed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scope": "project" if is_project else "global",
            "generated": False,
        })
    except Exception:
        pass


def _print_skill_installed_banner(
    skill_name: str,
    location: str,
    scope: str,
    source: str,
) -> None:
    """Print an ASCII art banner celebrating a successful skill install."""
    from rich import box
    from rich.panel import Panel

    ascii_art = (
        " ░██████╗██╗░░██╗██╗██╗░░░░░██╗░░░░░\n"
        " ██╔════╝██║░██╔╝██║██║░░░░░██║░░░░░\n"
        " ╚█████╗░█████═╝░██║██║░░░░░██║░░░░░\n"
        " ░╚═══██╗██╔═██╗░██║██║░░░░░██║░░░░░\n"
        " ██████╔╝██║░╚██╗██║███████╗███████╗\n"
        " ╚═════╝░╚═╝░░╚═╝╚═╝╚══════╝╚══════╝"
    )

    body = (
        f"[bold cyan]{ascii_art}[/bold cyan]\n\n"
        f"  [bold green]✓[/bold green] [bold]{skill_name}[/bold] installed\n\n"
        f"  [dim]📁 {location}[/dim]\n"
        f"  [dim]🔗 {source}[/dim]\n"
        f"  [dim]🌐 scope: {scope}[/dim]"
    )

    console.print()
    console.print(
        Panel(
            body,
            border_style="cyan",
            box=box.DOUBLE_EDGE,
            padding=(1, 3),
        )
    )
    console.print()


def _remove(
    skill_name: str,
    agent: str,
    project: bool = False,
    global_scope: bool = False,
    ask: bool = True,
    yes: bool = False,
) -> None:
    """Remove an installed skill.

    Args:
        skill_name: Name of the skill to remove.
        agent: Agent identifier for skills.
        project: If True, remove from project skills directory.
        global_scope: If True, remove from global skills directory.
        ask: If True and no scope flag given, prompt user interactively.
        yes: If True, skip confirmation prompt.
    """
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid skill name: {error_msg}")
        return

    skills_dir, is_project = _get_skills_dir(agent, project, global_scope, ask)
    if skills_dir is None:
        return

    skill_dir = skills_dir / skill_name
    is_valid_path, path_error = _validate_skill_path(skill_dir, skills_dir)
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        return

    if not skill_dir.exists():
        console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' not found at {skill_dir}")
        return

    if not yes:
        try:
            confirm = input(f"Remove skill '{skill_name}' from {skill_dir}? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\nCancelled.", style=COLORS["dim"])
            return
        if confirm not in {"y", "yes"}:
            console.print("Cancelled.", style=COLORS["dim"])
            return

    try:
        shutil.rmtree(skill_dir)
    except OSError as e:
        console.print(f"[bold red]Error:[/bold red] Could not remove skill directory: {e}")
        return

    try:
        from novacode_cli.skills.skill_lock import SkillLock
        SkillLock.for_skills_dir(skills_dir).remove(skill_name)
    except Exception:
        pass

    console.print(f"✓ Skill '{skill_name}' removed.", style=COLORS["primary"])


def _update(
    skill_name: str | None,
    agent: str,
    project: bool = False,
    global_scope: bool = False,
    all_skills: bool = False,
) -> None:
    """Update installed skill(s) by re-installing from their original source.

    Args:
        skill_name: Name of the skill to update (None when --all is used).
        agent: Agent identifier for skills.
        project: If True, target project skills directory.
        global_scope: If True, target global skills directory.
        all_skills: If True, update all skills recorded in the lock file.
    """
    if not skill_name and not all_skills:
        console.print(
            "[bold red]Error:[/bold red] Specify a skill name or pass --all."
        )
        return

    # When updating all, require explicit scope rather than prompting interactively
    ask = not all_skills
    skills_dir, is_project = _get_skills_dir(agent, project, global_scope, ask)
    if skills_dir is None:
        return

    from novacode_cli.skills.skill_lock import SkillLock
    lock = SkillLock.for_skills_dir(skills_dir)
    entries = lock.all_entries()

    if all_skills:
        if not entries:
            console.print(
                "[yellow]No skills in lock file. Nothing to update.[/yellow]\n"
                "[dim]Only skills installed via 'Nova skills add' are tracked.[/dim]"
            )
            return
        names_to_update = list(entries.keys())
        console.print(f"[dim]Updating {len(names_to_update)} skill(s)...[/dim]")
    else:
        is_valid, error_msg = _validate_name(skill_name)  # type: ignore[arg-type]
        if not is_valid:
            console.print(f"[bold red]Error:[/bold red] Invalid skill name: {error_msg}")
            return
        entry = lock.get(skill_name)  # type: ignore[arg-type]
        if entry is None:
            console.print(
                f"[yellow]Warning:[/yellow] Skill '{skill_name}' has no lock entry.\n"
                "[dim]Only skills installed via 'Nova skills add' can be updated.[/dim]"
            )
            return
        names_to_update = [skill_name]  # type: ignore[list-item]

    updated = 0
    for name in names_to_update:
        entry = entries.get(name)
        if not entry:
            console.print(f"[yellow]Skipping '{name}': no lock entry.[/yellow]")
            continue
        source_url = entry.get("source")
        if not source_url:
            console.print(f"[yellow]Skipping '{name}': lock entry has no source URL.[/yellow]")
            continue
        scope_project = entry.get("scope") == "project"
        override_name = entry.get("skill_name_override")
        try:
            console.print(f"[dim]Updating '{name}' from {source_url}...[/dim]")
            _add(
                url=source_url,
                agent=agent,
                project=scope_project,
                global_scope=not scope_project,
                force=True,
                skill_name=override_name or name,
            )
            updated += 1
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to update '{name}': {e}")

    console.print(f"\n✓ Updated {updated}/{len(names_to_update)} skill(s).", style=COLORS["primary"])


def _extract_skill_description(content: str) -> str | None:
    """Extract skill description from SKILL.md frontmatter.

    Args:
        content: SKILL.md content.

    Returns:
        Skill description if found in frontmatter, None otherwise.
    """
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 2:
        return None
    frontmatter = parts[1]
    match = re.search(r"^\s*description:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    if match:
        return match.group(1).strip().strip("\"'")
    return None


def _find(query: str) -> None:
    """Search GitHub for SKILL.md repositories matching query.

    Args:
        query: Search term (e.g., 'azure', 'kubernetes', 'pdf').
    """
    import requests

    if not query.strip():
        console.print("[bold red]Error:[/bold red] Search query cannot be empty.")
        return

    console.print(f"[dim]Searching for skills matching '{query}'...[/dim]")

    token = __import__("os").environ.get("GITHUB_TOKEN")
    headers: dict = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    results: list[dict] = []

    # Try GitHub code search first (finds SKILL.md files directly)
    try:
        resp = requests.get(
            "https://api.github.com/search/code",
            headers=headers,
            params={"q": f"{query} filename:SKILL.md", "per_page": 10},
            timeout=10,
        )
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                repo = item.get("repository", {})
                raw_url = (
                    item["html_url"]
                    .replace("github.com", "raw.githubusercontent.com")
                    .replace("/blob/", "/")
                )
                skill_name_found = None
                skill_desc = None
                try:
                    file_resp = requests.get(raw_url, headers=headers, timeout=8)
                    if file_resp.status_code == 200:
                        skill_name_found = _extract_skill_name(file_resp.text)
                        skill_desc = _extract_skill_description(file_resp.text)
                except requests.RequestException:
                    pass
                results.append({
                    "name": skill_name_found or repo.get("name", "(unknown)"),
                    "description": skill_desc or repo.get("description") or "(no description)",
                    "repo_url": repo.get("html_url", ""),
                })
        elif resp.status_code == 403:
            # Rate limited — fall back to repo search
            raise requests.RequestException("rate limited")
    except requests.RequestException:
        # Fallback: search repository descriptions/READMEs
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                headers=headers,
                params={"q": f"{query} SKILL.md in:readme", "per_page": 10},
                timeout=10,
            )
            if resp.status_code == 200:
                for repo in resp.json().get("items", []):
                    results.append({
                        "name": repo.get("name", "(unknown)"),
                        "description": repo.get("description") or "(no description)",
                        "repo_url": repo.get("html_url", ""),
                    })
            elif resp.status_code == 403:
                console.print(
                    "[yellow]GitHub search rate limited.[/yellow] "
                    "Set GITHUB_TOKEN env var to increase rate limit, or try again in 60s."
                )
                return
        except requests.RequestException as e:
            console.print(f"[bold red]Error:[/bold red] Network error: {e}")
            return

    if not results:
        console.print(f"[yellow]No skills found for '{query}'.[/yellow] Try a broader search term.")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style=f"bold cyan", box=None, padding=(0, 2))
    table.add_column("Skill", style="cyan", min_width=20)
    table.add_column("Description", style="dim", min_width=40)
    table.add_column("Repo", style="dim")

    for r in results:
        table.add_row(r["name"], r["description"][:80], r["repo_url"])

    console.print()
    console.print(table)
    console.print(f"\n[dim]Install with: Nova skills add <repo-url>[/dim]")


class _SkillsCommandParser(argparse.ArgumentParser):
    """Let upstream parse its own flags, including options before the source."""

    upstream_add = False

    def parse_known_args(
        self, args: list[str] | None = None, namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        """Keep the add command's argv intact for the official parser."""
        if not self.upstream_add:
            return super().parse_known_args(args, namespace)
        namespace = namespace or argparse.Namespace()
        namespace.upstream_args = list(args) if args is not None else sys.argv[1:]
        return namespace, []


def setup_skills_parser(
    subparsers: Any,
) -> argparse.ArgumentParser:
    """Setup the skills subcommand parser with all its subcommands."""
    skills_parser = subparsers.add_parser(
        "skills",
        help="Manage agent skills",
        description="Manage agent skills - create, list, and view skill information",
    )
    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command", help="Skills command", parser_class=_SkillsCommandParser,
    )

    # Skills list
    list_parser = skills_subparsers.add_parser(
        "list",
        help="List all available skills",
        description="List all available skills",
    )
    list_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    list_parser.add_argument(
        "--project",
        action="store_true",
        help="Show only project-level skills",
    )
    list_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Show only global skills (user-level)",
    )

    # Skills create
    create_parser = skills_subparsers.add_parser(
        "create",
        help="Create a new skill",
        description="Create a new skill with a template SKILL.md file",
    )
    create_parser.add_argument("name", help="Name of the skill to create (e.g., web-research)")
    create_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    create_parser.add_argument(
        "--project",
        action="store_true",
        help="Create skill in project directory instead of user directory",
    )
    create_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Create skill in global directory (user-level)",
    )

    # Skills info
    info_parser = skills_subparsers.add_parser(
        "info",
        help="Show detailed information about a skill",
        description="Show detailed information about a specific skill",
    )
    info_parser.add_argument("name", help="Name of the skill to show info for")
    info_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    info_parser.add_argument(
        "--project",
        action="store_true",
        help="Search only in project skills",
    )
    info_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Search only in global skills (user-level)",
    )

    # The add subcommand belongs to upstream; retain every argument verbatim.
    add_parser = skills_subparsers.add_parser(
        "add",
        help="Install skills using the official Skills CLI (requires Node.js/npx)",
        add_help=False,
    )
    add_parser.upstream_add = True
    # Skills remove
    remove_parser = skills_subparsers.add_parser(
        "remove",
        help="Remove an installed skill",
        description="Remove an installed skill and its directory",
    )
    remove_parser.add_argument(
        "name",
        help="Name of the skill to remove",
    )
    remove_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    remove_parser.add_argument(
        "--project",
        action="store_true",
        help="Remove from project skills directory",
    )
    remove_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Remove from global skills directory",
    )
    remove_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # Skills update
    update_parser = skills_subparsers.add_parser(
        "update",
        help="Update skill(s) from their original source",
        description="Re-fetch and reinstall a skill from its original GitHub URL",
    )
    update_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name of the skill to update (omit when using --all)",
    )
    update_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    update_parser.add_argument(
        "--project",
        action="store_true",
        help="Target project skills directory",
    )
    update_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Target global skills directory",
    )
    update_parser.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        help="Update all skills that have a lock entry",
    )

    # Skills find
    find_parser = skills_subparsers.add_parser(
        "find",
        help="Search GitHub for skills",
        description="Search GitHub for SKILL.md repositories matching a query",
    )
    find_parser.add_argument(
        "query",
        help="Search term (e.g. 'azure', 'kubernetes', 'pdf')",
    )

    # Skills search (alias for find)
    search_parser = skills_subparsers.add_parser(
        "search",
        help="Search GitHub for skills (alias for find)",
        description="Search GitHub for SKILL.md repositories matching a query",
    )
    search_parser.add_argument(
        "query",
        help="Search term (e.g. 'azure', 'kubernetes', 'pdf')",
    )

    return skills_parser

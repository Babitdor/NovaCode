"""Handler for /skill:<name> direct skill invocation.

When the user types /skill:<name>, we check if the name matches a known
skill. If it does, we read the skill's SKILL.md and return its content
as a prompt for the agent to follow.

Examples:
    /skill:api-testing          → invoke the "api-testing" skill
    /skill:docker-deploy        → invoke the "docker-deploy" skill
    /skill:code-review fix.py   → invoke "code-review" with "fix.py" as args
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novacode_cli.config.config import Settings
from novacode_cli.skills.load import list_skills


@dataclass
class SkillInvocation:
    """A resolved skill ready to run — presentation-agnostic.

    ``prompt`` is fed to the agent; the rest is metadata each UI renders its own
    way (legacy REPL via rich console, TUI via native widgets).
    """

    prompt: str
    name: str
    source: str  # "project" | "global"
    description: str
    args: str | None = None
    supporting_files: list[str] = field(default_factory=list)
    executable: str | None = None  # human-readable description if the skill is runnable
    pinned_prompt: str | None = None  # middleware loads a structured snapshot on this turn

# File extensions to skip when scanning for supporting files
_SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip",
                    ".tar", ".gz", ".exe", ".bin"}
_MAX_SUPPORTING_FILE_SIZE = 100_000  # 100 KB
# Known non-content directories to skip
_SKIP_DIRS = {".git", ".github", ".vscode", "__pycache__", "node_modules", ".venv", "dist", "build"}
# Root-level metadata files that belong to the repo, not the skill content
_ROOT_SKIP_FILES = {"README.md", "LICENSE.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
                    "CHANGELOG.md", "SECURITY.md", ".gitignore"}


async def _try_skill_invocation(
    cmd: str,
    cmd_args: str | None,
    session_state: Any,
    assistant_id: str,
) -> SkillInvocation | None:
    """Resolve ``/cmd`` to a skill without blocking the event loop.

    The lookup scans every skill directory (hundreds of SKILL.md files, plus a
    heavy first import) and may run an executable skill: done on the UI loop
    it froze the whole TUI for seconds on any unrecognised slash command.
    """
    import asyncio

    return await asyncio.to_thread(
        _resolve_skill_invocation, cmd, cmd_args, session_state, assistant_id
    )


def _resolve_skill_invocation(
    cmd: str,
    cmd_args: str | None,
    session_state: Any,
    assistant_id: str,
) -> SkillInvocation | None:
    """Try to resolve a skill by name (presentation-free).

    Checks if ``cmd`` matches a known skill name (from user-skills or
    project-skills directories). If found, reads the SKILL.md and returns a
    :class:`SkillInvocation` (the agent prompt plus the metadata each UI renders
    itself). Returns ``None`` if no skill matches (or its SKILL.md is empty) so
    the caller can fall back to "unknown command".

    Args:
        cmd: The command name (without the leading ``/``).
        cmd_args: Optional arguments after the skill name.
        session_state: Current session state (for workspace_root).
        assistant_id: Agent identifier (for user skills directory).

    Returns:
        A :class:`SkillInvocation` if the skill was found, or ``None``.
    """
    settings = Settings.from_environment()
    in_project = settings.project_root is not None

    # Collect skills from all sources
    user_skills_dir = settings.ensure_user_skills_dir(assistant_id)
    project_skills_dir = settings.get_project_skills_dir() if in_project else None
    claude_skills_dir = Settings.get_global_claude_skills_dir()

    # Skills shipped by installed Claude-compatible plugins carry an explicit
    # on-disk ``path``, so _read_skill_content/_find_skill_dir resolve them
    # (path-first) without extra params.
    from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

    plugin_skills_dirs = [d for _, d in plugin_skill_dirs()]

    skills = list_skills(
        user_skills_dir=user_skills_dir,
        shared_skills_dir=Settings.get_shared_skills_dir(),
        project_skills_dirs=settings.get_project_skills_dirs(),
        claude_skills_dir=claude_skills_dir if claude_skills_dir.exists() else None,
        project_skills_dir=project_skills_dir,
        plugin_skills_dirs=plugin_skills_dirs,
    )

    if not skills:
        return None

    # Normalize the command to match skill names (hyphens = hyphens)
    # Skill names use hyphens (e.g. "api-testing"), but users might
    # type underscores or spaces.  Normalize to hyphens for matching.
    cmd_normalized = cmd.replace("_", "-").strip().lower()

    # Find matching skill
    matched_skill = None
    for skill in skills:
        skill_name = skill.get("name", "").lower()
        if skill_name == cmd_normalized:
            matched_skill = skill
            break

    if matched_skill is None:
        return None
    from novacode_cli.skills.skills_prefs import effective_disabled

    if matched_skill["name"] in effective_disabled():
        return None

    # Found a matching skill — read its SKILL.md
    skill_name = matched_skill.get("name", cmd)

    # The skill's SKILL.md content
    skill_content = _read_skill_content(matched_skill, user_skills_dir, project_skills_dir, claude_skills_dir)
    if not skill_content:
        # Found but empty — treat as no match so the caller falls back.
        return None

    # Resolve display metadata (rendered by the caller, not here).
    source = matched_skill.get("source", "unknown")
    source_label = "project" if source == "project" else "global"
    description = matched_skill.get("description", "")
    description_short = description[:80] + "..." if len(description) > 80 else description

    # Build the prompt that the agent will process
    extra_context = ""
    if cmd_args:
        extra_context = f"\n\nUser provided additional context: {cmd_args}"

    # Discover supporting files in the skill directory
    skill_dir = _find_skill_dir(matched_skill, user_skills_dir, project_skills_dir, claude_skills_dir)
    supporting_files = _get_supporting_files(skill_dir) if skill_dir else {}

    # Detect a runnable executable (skill.py / package/ / pyproject entrypoint).
    executable_desc: str | None = None
    if skill_dir is not None:
        from novacode_cli.skills.executable import find_executable

        exe = find_executable(skill_dir)
        if exe is not None:
            executable_desc = exe.describe()

    # ``--run`` executes the skill's executable directly and returns its output
    # as the prompt, instead of feeding the markdown to the agent.
    if executable_desc is not None and cmd_args and cmd_args.strip().startswith("--run"):
        return _run_skill_executable(
            skill_dir, cmd_args, skill_name, source_label, description_short, executable_desc
        )

    prompt_parts = [
        "Follow the instructions in the skill below.\n\n",
        f"--- SKILL: {skill_name} ---\n\n",
        skill_content,
    ]

    if supporting_files:
        prompt_parts.append("\n\n### Supporting files\n\n")
        prompt_parts.append(
            "This skill ships with supporting reference files listed below. "
            "Load only the references needed for this task, relative to the skill directory.\n"
        )
        for rel_path in supporting_files:
            prompt_parts.append(f"- {rel_path}\n")

    prompt_parts.append(f"--- END SKILL: {skill_name} ---")
    prompt_parts.append(extra_context)
    prompt_parts.append(
        "\n\nExecute the skill above. Apply it to the current project and context."
    )

    prompt = "".join(prompt_parts)

    return SkillInvocation(
        prompt=prompt,
        name=skill_name,
        source=source_label,
        description=description_short,
        args=cmd_args,
        supporting_files=sorted(supporting_files.keys()),
        executable=executable_desc,
        pinned_prompt=f"${skill_name}" + (f" {cmd_args}" if cmd_args else ""),
    )


def _run_skill_executable(
    skill_dir: Path,
    cmd_args: str,
    skill_name: str,
    source_label: str,
    description_short: str,
    executable_desc: str,
) -> SkillInvocation:
    """Run a skill's executable for ``/skill:<name> --run`` and return its output.

    The executable's stdout/stderr becomes the ``prompt`` so the agent sees the
    result directly, instead of being asked to follow the markdown.
    """
    from novacode_cli.skills.executable import run_executable

    run_args = cmd_args.strip()[len("--run"):].strip().split()
    result = run_executable(skill_dir, run_args)
    if result["ok"]:
        run_prompt = (
            f"The skill '{skill_name}' was executed directly.\n\n"
            f"Command: {' '.join(result['command'])}\n\n"
            f"Output:\n{result['output']}"
        )
    else:
        run_prompt = (
            f"The skill '{skill_name}' executable failed.\n\n"
            f"Command: {' '.join(result['command'])}\n\n"
            f"Error: {result['error']}\n\n"
            f"Output:\n{result['output']}"
        )
    return SkillInvocation(
        prompt=run_prompt,
        name=skill_name,
        source=source_label,
        description=description_short,
        args=cmd_args,
        executable=executable_desc,
    )


def _find_skill_dir(
    skill: dict[str, Any],
    user_skills_dir: Path | None,
    project_skills_dir: Path | None,
    claude_skills_dir: Path | None = None,
) -> Path | None:
    """Find the skill's directory on disk.

    Tries path attribute, user skills dir, claude skills dir, then project skills dir.
    Returns None if the directory cannot be determined or doesn't exist.
    """
    skill_name = skill.get("name", "")

    # Try the skill's path attribute first (may be the directory itself)
    skill_path = skill.get("path", "")
    if skill_path:
        p = Path(skill_path)
        if p.is_dir():
            return p
        # path might be the SKILL.md file itself — parent is the skill dir
        if p.is_file() and p.name == "SKILL.md":
            return p.parent

    # Try user skills directory
    if user_skills_dir and user_skills_dir.exists():
        candidate = user_skills_dir / skill_name
        if candidate.is_dir():
            return candidate

    # Try claude skills directory
    if claude_skills_dir and claude_skills_dir.exists():
        candidate = claude_skills_dir / skill_name
        if candidate.is_dir():
            return candidate

    # Try project skills directory
    if project_skills_dir and project_skills_dir.exists():
        candidate = project_skills_dir / skill_name
        if candidate.is_dir():
            return candidate

    return None


def _get_supporting_files(skill_dir: Path) -> dict[str, str]:
    """Scan a skill directory for supporting files alongside SKILL.md.

    Return a bounded filename inventory. File contents load only on demand.
    """
    supporting: dict[str, str] = {}

    if not skill_dir.is_dir():
        return supporting

    import os

    visited = 0
    pending = [skill_dir]
    while pending and visited < 2000 and len(supporting) < 100:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 2000 or len(supporting) >= 100:
                        break
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    item = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIP_DIRS:
                            pending.append(item)
                        continue
                    if not entry.is_file(follow_symlinks=False) or entry.name == "SKILL.md":
                        continue
                    rel = item.relative_to(skill_dir)
                    if len(rel.parts) == 1 and entry.name in _ROOT_SKIP_FILES:
                        continue
                    if item.suffix.lower() not in _SKIP_EXTENSIONS:
                        supporting[rel.as_posix()] = ""
        except OSError:
            continue

    return supporting


def _read_skill_content(
    skill: dict[str, Any],
    user_skills_dir: Path | None,
    project_skills_dir: Path | None,
    claude_skills_dir: Path | None = None,
) -> str | None:
    """Read a skill's SKILL.md content from disk.

    Tries multiple locations: the skill's path attribute, user skills
    directory, claude skills directory, then project skills directory.

    Args:
        skill: Skill metadata dict (must have 'name' key).
        user_skills_dir: Path to user-level skills directory.
        project_skills_dir: Path to project-level skills directory.
        claude_skills_dir: Path to global Claude Code skills directory.

    Returns:
        SKILL.md content as string, or None if not found.
    """
    skill_name = skill.get("name", "")

    # Try the skill's path attribute first (may contain the full path)
    skill_path = skill.get("path", "")
    if skill_path:
        skill_md = Path(skill_path)
        if skill_md.is_file():
            try:
                return skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        # If path is a directory, look for SKILL.md inside it
        if skill_md.is_dir():
            md_path = skill_md / "SKILL.md"
            if md_path.is_file():
                try:
                    return md_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass

    # Try user skills directory
    if user_skills_dir and user_skills_dir.exists():
        md_path = user_skills_dir / skill_name / "SKILL.md"
        if md_path.is_file():
            try:
                return md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

    # Try claude skills directory
    if claude_skills_dir and claude_skills_dir.exists():
        md_path = claude_skills_dir / skill_name / "SKILL.md"
        if md_path.is_file():
            try:
                return md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

    # Try project skills directory
    if project_skills_dir and project_skills_dir.exists():
        md_path = project_skills_dir / skill_name / "SKILL.md"
        if md_path.is_file():
            try:
                return md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

    return None

"""Skill loader for parsing and loading agent skills from SKILL.md files.

This module implements Anthropic's agent skills pattern with YAML frontmatter parsing.
Each skill is a directory containing a SKILL.md file with:
- YAML frontmatter (name, description required)
- Markdown instructions for the agent
- Optional supporting files (scripts, configs, etc.)

Example SKILL.md structure:
```markdown
---
name: web-research
description: Structured approach to conducting thorough web research
---

# Web Research Skill

## When to Use
- User asks you to research a topic
...
```
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Lazy deepagents imports: `list_skills` pulls in deepagents + the filesystem
# backend (~4s). We defer them to first call so importing this module (and the
# modules that import it, e.g. skills.skill_creation) doesn't pay that cost at
# CLI startup. `SkillMetadata` stays importable via a lazy proxy below.
if TYPE_CHECKING:
    from deepagents.middleware.skills import SkillMetadata

if TYPE_CHECKING:
    from pathlib import Path

# Maximum size for SKILL.md files (10MB)
MAX_SKILL_FILE_SIZE = 10 * 1024 * 1024


class _LazySkillMetadata:
    """Lazy proxy so ``from novacode_cli.skills.load import SkillMetadata`` works
    without importing deepagents at module load. Resolves on first attribute
    access (used only as a type in annotations and dict values)."""

    def __getattr__(self, name: str):
        from deepagents.middleware.skills import SkillMetadata

        return getattr(SkillMetadata, name)


SkillMetadata = _LazySkillMetadata()  # type: ignore[assignment]

# Re-export for CLI commands
__all__ = ["SkillMetadata", "find_skill_dir", "list_skills"]


def find_skill_dir(name: str) -> tuple[Path, str] | None:
    """Locate an installed skill's directory by name.

    Searches the writable sources in the same precedence order as
    :func:`list_skills` (user → project → global Claude) and returns
    ``(skill_dir, source)`` where ``source`` is ``"user"`` / ``"project"`` /
    ``"claude"``, or ``None`` when no such skill exists.

    Unlike :func:`list_skills` this reads only the filesystem — no deepagents
    import — so callers that just need a path (prune, delete, edit) stay cheap.
    The returned path is the *directory*; the skill file is ``dir / "SKILL.md"``.
    """
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()

    # User (global) skills first — the writable foundation.
    user_dir = settings.get_global_skills_dir()
    if (user_dir / name / "SKILL.md").is_file():
        return user_dir / name, "user"

    for project_dir in settings.get_project_skills_dirs():
        if (project_dir / name / "SKILL.md").is_file():
            return project_dir / name, "project"

    claude_dir = Settings.get_global_claude_skills_dir()
    if (claude_dir / name / "SKILL.md").is_file():
        return claude_dir / name, "claude"

    return None


def list_skills(
    *,
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    claude_skills_dir: Path | None = None,
    plugin_skills_dirs: list[Path] | None = None,
) -> list[SkillMetadata]:
    """List skills from user, project, and/or global Claude directories.

    This is a CLI-specific wrapper around the prebuilt middleware's skill loading
    functionality. It uses FilesystemBackend to load skills from local directories.

    Sources are loaded in this order (later overrides earlier):
    1. User skills (~/.nova/skills/) — foundation
    2. Global Claude skills (~/.claude/skills/) — shared Claude Code skills
    3. Project skills (.nova/skills/ or .claude/skills/) — highest priority

    When multiple sources have skills with the same name, the later source's
    skill takes precedence. Each skill includes a 'source' field indicating
    its origin ('user', 'claude', or 'project').

    Args:
        user_skills_dir: Path to the user-level skills directory.
        project_skills_dir: Path to the project-level skills directory.
        claude_skills_dir: Path to the global Claude Code skills directory
            (~/.claude/skills/).

    Returns:
        Merged list of skill metadata from all sources, with later sources
        taking precedence over earlier ones when names conflict. Each skill
        includes a 'source' field indicating its origin.
    """
    # Lazy deepagents imports (see module docstring): only needed when actually
    # listing skills, not at import time.
    from deepagents.middleware.skills import SkillMetadata
    from deepagents.middleware.skills import _list_skills as list_skills_from_backend
    from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend

    all_skills: dict[str, SkillMetadata] = {}

    # Load user skills first (foundation)
    if user_skills_dir and user_skills_dir.exists():
        user_backend = FilesystemBackend(root_dir=str(user_skills_dir), virtual_mode=True)
        user_skills = list_skills_from_backend(
            backend=user_backend, source_path="."
        )
        for skill in user_skills:
            skill["source"] = "user"  # type: ignore[typeddict-unknown-key]
            all_skills[skill["name"]] = skill

    # Load global Claude Code skills second (override/supplement user skills)
    if claude_skills_dir and claude_skills_dir.exists():
        claude_backend = FilesystemBackend(root_dir=str(claude_skills_dir), virtual_mode=True)
        claude_skills = list_skills_from_backend(
            backend=claude_backend, source_path="."
        )
        for skill in claude_skills:
            skill["source"] = "claude"  # type: ignore[typeddict-unknown-key]
            all_skills[skill["name"]] = skill

    # Load installed-plugin skills (~/.nova/plugins/<name>/skills). Set an explicit
    # on-disk ``path`` so the invoker's path-first read/dir resolution works without
    # threading plugin dirs through every helper.
    for plugin_dir in plugin_skills_dirs or []:
        if not (plugin_dir and plugin_dir.exists()):
            continue
        plugin_backend = FilesystemBackend(root_dir=str(plugin_dir), virtual_mode=True)
        for skill in list_skills_from_backend(backend=plugin_backend, source_path="."):
            skill["source"] = "plugin"  # type: ignore[typeddict-unknown-key]
            # The backend's ``path`` is virtual (rooted at plugin_dir, e.g.
            # "/foo/SKILL.md") and uses the real directory name — which can differ
            # from the frontmatter name. Map it to an absolute on-disk path so the
            # invoker's path-first resolution works.
            vpath = str(skill.get("path", "") or "").lstrip("/\\")
            skill["path"] = str(plugin_dir / vpath) if vpath else str(plugin_dir / skill["name"])  # type: ignore[typeddict-unknown-key]
            all_skills[skill["name"]] = skill

    # Load project skills last (override/augment)
    if project_skills_dir and project_skills_dir.exists():
        project_backend = FilesystemBackend(root_dir=str(project_skills_dir), virtual_mode=True)
        project_skills = list_skills_from_backend(
            backend=project_backend, source_path="."
        )
        for skill in project_skills:
            skill["source"] = "project"  # type: ignore[typeddict-unknown-key]
            all_skills[skill["name"]] = skill

    return list(all_skills.values())

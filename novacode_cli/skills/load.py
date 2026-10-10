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

    Searches installed sources with the same last-source-wins precedence as
    :func:`list_skills` (user → shared → Claude → plugins → project) and returns
    ``(skill_dir, source)`` where ``source`` is ``"user"`` / ``"project"`` /
    ``"claude"`` / ``"plugin"``, or ``None`` when no such skill exists.

    Unlike :func:`list_skills` this reads only the filesystem — no deepagents
    import — so callers that just need a path (prune, delete, edit) stay cheap.
    The returned path is the *directory*; the skill file is ``dir / "SKILL.md"``.
    """
    import re

    from novacode_cli.config.config import Settings

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        return None
    settings = Settings.from_environment()
    from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

    sources = [
        (settings.get_global_skills_dir(), "user"),
        (Settings.get_shared_skills_dir(), "user"),
        (Settings.get_global_claude_skills_dir(), "claude"),
        *((path, "plugin") for _, path in plugin_skill_dirs()),
        *((path, "project") for path in settings.get_project_skills_dirs()),
    ]
    # Later sources win, consistently with discovery and the agent backend.
    for directory, source in reversed(sources):
        candidate = directory / name
        if (candidate / "SKILL.md").is_file():
            return candidate, source

    return None


def _dir_signature(directory: Path) -> frozenset | None:
    """Validate current content, including edits with restored timestamps."""
    import os
    from pathlib import Path

    from novacode_cli.computation_cache import digest

    sig = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path) / "SKILL.md"
                try:
                    sig.append((entry.name, digest(path.read_bytes())))
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
    except OSError:
        return None
    return frozenset(sig)


def _list_dir(directory: Path) -> list:
    """Reuse validated directory listings in memory, with defensive copies."""
    from time import perf_counter_ns
    from typing import cast

    from deepagents.middleware.skills import _list_skills as list_skills_from_backend

    from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend
    from novacode_cli.computation_cache import digest, get, put, record_timing

    started = perf_counter_ns()
    signature = _dir_signature(directory)
    record_timing("skill_definitions", "validation", perf_counter_ns() - started)
    key = digest(repr(("skill-listing-v1", str(directory.resolve()), sorted(signature or ()))))
    cached = get("skill_definitions", key) if signature is not None else None
    if cached is not None and signature == _dir_signature(directory):
        return cast("list", cached)
    backend = FilesystemBackend(root_dir=str(directory), virtual_mode=True)
    started = perf_counter_ns()
    skills = list_skills_from_backend(backend=backend, source_path=".")
    record_timing("skill_definitions", "computation", perf_counter_ns() - started)
    # If files changed during the parse, do not associate it with the old key.
    if signature is not None and signature == _dir_signature(directory):
        put("skill_definitions", key, skills)
    return skills


def list_skills(
    *,
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    claude_skills_dir: Path | None = None,
    plugin_skills_dirs: list[Path] | None = None,
    shared_skills_dir: Path | None = None,
    project_skills_dirs: list[Path] | None = None,
) -> list[SkillMetadata]:
    """List skills from user, project, and/or global Claude directories.

    This is a CLI-specific wrapper around the prebuilt middleware's skill loading
    functionality. It uses FilesystemBackend to load skills from local directories.

    Sources are loaded in this order (later overrides earlier):
    1. User skills (~/.nova/skills/) — foundation
    2. Shared global skills (~/.agents/skills/)
    3. Global Claude skills (~/.claude/skills/) — shared Claude Code skills
    4. Project skills (.agents/skills/, .nova/skills/, .claude/skills/) — highest priority

    When multiple sources have skills with the same name, the later source's
    skill takes precedence. Each skill includes a 'source' field indicating
    its origin ('user', 'claude', or 'project').

    Args:
        user_skills_dir: Path to the user-level skills directory.
        project_skills_dir: Path to the project-level skills directory.
        claude_skills_dir: Path to the global Claude Code skills directory
            (~/.claude/skills/).
        shared_skills_dir: Global canonical directory used by the Skills CLI.
        project_skills_dirs: Project directories in increasing precedence order.
        plugin_skills_dirs: Skill directories supplied by installed plugins.

    Returns:
        Merged list of skill metadata from all sources, with later sources
        taking precedence over earlier ones when names conflict. Each skill
        includes a 'source' field indicating its origin.
    """
    # Lazy deepagents imports (see module docstring): only needed when actually
    # listing skills, not at import time.

    all_skills: dict[str, SkillMetadata] = {}

    # Load user skills first (foundation)
    if user_skills_dir and user_skills_dir.exists():
        user_skills = _list_dir(user_skills_dir)
        for skill in user_skills:
            skill["source"] = "user"  # type: ignore[typeddict-unknown-key]
            skill["path"] = str(user_skills_dir / str(skill["path"]).lstrip("/\\"))
            all_skills[skill["name"]] = skill

    # Shared skills keep absolute paths so invocation also finds supporting files.
    if shared_skills_dir and shared_skills_dir.is_dir():
        for skill in _list_dir(shared_skills_dir):
            skill["source"] = "user"
            virtual_path = str(skill.get("path", "")).lstrip("/\\")
            skill["path"] = str(shared_skills_dir / virtual_path)
            all_skills[skill["name"]] = skill

    # Load global Claude Code skills second (override/supplement user skills)
    if claude_skills_dir and claude_skills_dir.exists():
        claude_skills = _list_dir(claude_skills_dir)
        for skill in claude_skills:
            skill["source"] = "claude"  # type: ignore[typeddict-unknown-key]
            skill["path"] = str(claude_skills_dir / str(skill["path"]).lstrip("/\\"))
            all_skills[skill["name"]] = skill

    # Load installed-plugin skills (~/.nova/plugins/<name>/skills). Set an explicit
    # on-disk ``path`` so the invoker's path-first read/dir resolution works without
    # threading plugin dirs through every helper.
    for plugin_dir in plugin_skills_dirs or []:
        if not (plugin_dir and plugin_dir.exists()):
            continue
        for skill in _list_dir(plugin_dir):
            skill["source"] = "plugin"  # type: ignore[typeddict-unknown-key]
            # The backend's ``path`` is virtual (rooted at plugin_dir, e.g.
            # "/foo/SKILL.md") and uses the real directory name — which can differ
            # from the frontmatter name. Map it to an absolute on-disk path so the
            # invoker's path-first resolution works.
            vpath = str(skill.get("path", "") or "").lstrip("/\\")
            skill["path"] = str(plugin_dir / vpath) if vpath else str(plugin_dir / skill["name"])  # type: ignore[typeddict-unknown-key]
            all_skills[skill["name"]] = skill

    # Project sources override global sources. Include legacy callers' single root.
    directories = list(project_skills_dirs or [])
    if project_skills_dir and project_skills_dir not in directories:
        directories.append(project_skills_dir)
    for directory in directories:
        if not directory.is_dir():
            continue
        for skill in _list_dir(directory):
            skill["source"] = "project"
            virtual_path = str(skill.get("path", "")).lstrip("/\\")
            skill["path"] = str(directory / virtual_path)
            all_skills[skill["name"]] = skill

    return list(all_skills.values())

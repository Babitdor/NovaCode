"""Explicit, role-specific skill libraries shared by foreground and background agents."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

SPECIALIST_LIBRARIES: dict[str, tuple[str, ...]] = {
    "code-explorer": ("codebase-explorer", "graphify"),
    "plan-scout-agent": ("codebase-explorer", "graphify"),
    "refactoring-specialist-agent": ("improve-codebase-architecture", "refactoring"),
    "refactoring-agent": ("improve-codebase-architecture", "refactoring"),
    "bug-fix-agent": ("systematic-debugging",),
    "bug-investigation-agent": ("systematic-debugging",),
    "browser-automation-agent": ("agent-browser", "browser-use", "web-research"),
    "code-review-agent": ("code-review", "code-reviewer"),
    "security-audit-agent": ("security-audit", "security-review", "security-best-practices"),
    "dependency-audit-agent": ("dependency-audit", "security-audit"),
    "performance-audit-agent": ("performance-audit", "profiling", "systematic-debugging"),
    "test-generation-agent": ("test-driven-development", "testing", "pytest"),
    "test-runner-agent": ("testing", "pytest", "verification-before-completion"),
    "documentation-update-agent": ("documentation", "technical-writer"),
    "research-agent": ("web-research", "deep-research"),
    "web-researcher": ("web-research", "deep-research"),
    "fact-checker": ("fact-checking", "web-research"),
    "research-synthesizer": ("research-synthesis", "web-research"),
    "literature-reviewer": ("literature-review", "academic-research"),
    "market-analyst": ("market-research",),
    "financial-analyst": ("financial-analysis",),
    "technical-researcher": ("web-research", "technical-research"),
}


def library_names(name: str, definition: dict | None = None) -> tuple[str, ...] | None:
    """None means full library; [] means no skills; unknown specialists opt in explicitly."""
    definition = definition or {}
    if "skill_names" in definition:
        chosen = definition["skill_names"]
        if chosen == "*":
            return None
        if not isinstance(chosen, list) or any(
            not isinstance(value, str)
            or len(value) > 64
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)
            for value in chosen
        ):
            message = "skill_names must be a list of kebab-case skill names, or '*'"
            raise ValueError(message)
        return tuple(dict.fromkeys(chosen))
    if name in {"general-purpose", "general-purpose-async"}:
        return None
    return SPECIALIST_LIBRARIES.get(name, ())


class LibraryDiscoveryBackend:
    """Filter directory discovery before parsing files, without changing file permissions."""

    def __init__(self, backend: object, names: tuple[str, ...]) -> None:
        self.backend = backend
        self.names = frozenset(names)

    def ls(self, path: str):
        from deepagents.backends.protocol import LsResult

        result = self.backend.ls(path)
        entries = result.entries if isinstance(result, LsResult) else result
        selected = [
            item
            for item in entries or []
            if PurePosixPath(item["path"].replace("\\", "/")).name in self.names
        ]
        return (
            LsResult(entries=selected, error=result.error)
            if isinstance(result, LsResult)
            else selected
        )

    def __getattr__(self, name: str):
        return getattr(self.backend, name)


def background_library(name: str, definition: dict | None = None):
    """Build fresh discovery and activation middleware for one background graph."""
    from deepagents.backends import CompositeBackend

    from novacode_cli.agents.async_workspace import workspace_root
    from novacode_cli.backends import OptimizedFilesystemBackend
    from novacode_cli.config.config import Settings, settings
    from novacode_cli.plugins.claude_plugins import plugin_skill_dirs
    from novacode_cli.skills.refreshing_middleware import SubagentSkillsMiddleware

    root = workspace_root()
    directories = [
        settings.get_global_skills_dir(),
        Settings.get_shared_skills_dir(),
        Settings.get_global_claude_skills_dir(),
        *(directory for _, directory in plugin_skill_dirs()),
        *(root / marker / "skills" for marker in (".agents", ".claude", ".nova")),
    ]
    directories = list(dict.fromkeys(directory for directory in directories if directory.is_dir()))
    routes = {
        f"/agent-skills-{index}/": OptimizedFilesystemBackend(root_dir=directory, virtual_mode=True)
        for index, directory in enumerate(directories)
    }
    # The graph's file/shell backend remains its workspace backend. Only the
    # skill helpers use these extra mounts; they check the selected library.
    backend = CompositeBackend(
        default=OptimizedFilesystemBackend(root_dir=root, virtual_mode=True), routes=routes
    )
    return SubagentSkillsMiddleware(
        backend=backend,
        sources=list(routes),
        watch_dirs=directories,
        library_names=library_names(name, definition),
    )

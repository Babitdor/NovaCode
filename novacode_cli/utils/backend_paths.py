"""Virtual path utilities for backend routing.

This module provides centralized conversion between virtual paths (used by the
LLM and the CompositeBackend) and real filesystem paths. All subsystems that
need to translate between these two path styles should use these utilities
instead of ad-hoc path manipulation.

Virtual Path Routes:
    /memories/          → ~/.nova/{agent_id}/        (user memory, agent.md)
    /project-memory/    → {workspace_root}/.nova/    (project memory, NOVA.md)
    /.nova/plans/       → {workspace_root}/.nova/plans/ (plan files)
    /skills/            → ~/.nova/skills/            (global skills)
    /project-skills-{i}/ → project skill dirs
    /                   → {workspace_root}            (workspace root)

Usage:
    from novacode_cli.utils.backend_paths import (
        virtual_to_real_path,
        real_to_virtual_path,
        find_latest_plan_file_virtual,
        read_via_backend,
        write_via_backend,
    )
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Virtual path route definitions
# ---------------------------------------------------------------------------

# Maps virtual path prefixes to their corresponding real directory patterns.
# The CompositeBackend uses these same prefixes to route file operations.
VIRTUAL_ROUTES: dict[str, str] = {
    "/memories/": "~/.nova/{agent_id}/",
    "/project-memory/": "{workspace_root}/.nova/",
    "/.nova/plans/": "{workspace_root}/.nova/plans/",
    "/skills/": "~/.nova/skills/",
}


def virtual_to_real_path(
    virtual_path: str,
    *,
    agent_id: str | None = None,
    workspace_root: Path | None = None,
    backend: BackendProtocol | None = None,
) -> Path | None:
    """Convert a virtual path to a real filesystem path.

    Uses the CompositeBackend's routing when available, or falls back to
    manual path resolution based on known route prefixes.

    Args:
        virtual_path: Virtual path starting with / (e.g., "/memories/agent.md").
        agent_id: Agent identifier for /memories/ route resolution.
        workspace_root: Project root directory for /project-memory/ and
            /.nova/plans/ route resolution.
        backend: Optional CompositeBackend for routing. When provided,
            the backend handles path resolution.

    Returns:
        Real filesystem Path, or None if the path cannot be resolved.
    """
    if not virtual_path or not virtual_path.startswith("/"):
        return None

    from novacode_cli.security.paths import resolve_beneath, virtual_relative_path

    try:
        virtual_relative_path(virtual_path)
    except ValueError:
        return None

    def confined(root: Path, key: str) -> Path | None:
        try:
            return resolve_beneath(Path(root), key)
        except (ValueError, OSError, RuntimeError):
            return None

    def from_backend(selected, key: str) -> Path | None:
        root = getattr(selected, "cwd", None) or getattr(selected, "root_dir", None)
        if root is None:
            return None
        target = confined(Path(root), key)
        if target is None:
            return None
        resolver = getattr(selected, "_resolve_path", None)
        if callable(resolver):
            resolved = Path(resolver(key)).resolve()
            return resolved if resolved == target else None
        return target

    # When backend is available, let it handle the resolution.
    # The backend's FilesystemBackend._resolve_path() converts virtual
    # paths to real paths using its root_dir and virtual_mode settings.
    if backend is not None:
        try:
            router = getattr(backend, "_get_backend_and_key", None)
            if callable(router):
                selected, key = router(virtual_path)
                return from_backend(selected, key)
            # Try to use the backend's internal path resolution.
            # CompositeBackend routes to the correct sub-backend.
            if hasattr(backend, "default"):
                # CompositeBackend — find the right sub-backend for this path.
                for prefix, sub_backend in _get_routes(backend):
                    if virtual_path.startswith(prefix):
                        # Strip the prefix and resolve within the sub-backend.
                        relative = "/" + virtual_path[len(prefix) :].lstrip("/")
                        return from_backend(sub_backend, relative)
                # Fall through to default backend.
                default = backend.default
                return from_backend(default, virtual_path)
            elif hasattr(backend, "root_dir") or hasattr(backend, "cwd"):
                return from_backend(backend, virtual_path)
            return None
        except Exception:
            logger.debug("backend path resolution failed for %s", virtual_path)
            return None

    # Manual fallback: resolve based on known route prefixes.
    if virtual_path.startswith("/memories/"):
        if not agent_id:
            return None
        from novacode_cli.config.config import MAIN_AGENT_ID, settings

        aid = agent_id or MAIN_AGENT_ID
        agent_dir = settings.get_agent_dir(aid)
        relative = virtual_path.removeprefix("/memories/").lstrip("/")
        return confined(agent_dir, relative)

    for prefix, getter in (
        ("/skills/", "get_user_skills_dir"),
        ("/claude-skills/", "get_global_claude_skills_dir"),
        ("/shared-skills/", "get_shared_skills_dir"),
    ):
        if virtual_path.startswith(prefix):
            from novacode_cli.config.config import settings

            return confined(getattr(settings, getter)(), virtual_path[len(prefix):])

    if virtual_path.startswith("/project-memory/"):
        if not workspace_root:
            from novacode_cli.config.config import settings

            workspace_root = settings.project_root or Path.cwd()
        relative = virtual_path.removeprefix("/project-memory/").lstrip("/")
        return confined(workspace_root / ".nova", relative)

    if virtual_path.startswith("/.nova/plans/"):
        if not workspace_root:
            from novacode_cli.config.config import settings

            workspace_root = settings.project_root or Path.cwd()
        relative = virtual_path.removeprefix("/.nova/plans/").lstrip("/")
        return confined(workspace_root / ".nova" / "plans", relative)

    # Default: workspace root
    if not workspace_root:
        from novacode_cli.config.config import settings

        workspace_root = settings.project_root or Path.cwd()
    return confined(workspace_root, virtual_path)


def real_to_virtual_path(
    real_path: Path,
    *,
    agent_id: str | None = None,
    workspace_root: Path | None = None,
) -> str | None:
    """Convert a real filesystem path to a virtual path.

    Matches the real path against known route directories and returns
    the corresponding virtual path.

    Args:
        real_path: Real filesystem path.
        agent_id: Agent identifier for /memories/ route.
        workspace_root: Project root directory.

    Returns:
        Virtual path string (e.g., "/memories/agent.md"), or None if
        the path doesn't match any known route.
    """
    from novacode_cli.config.config import MAIN_AGENT_ID, settings

    if workspace_root is None:
        workspace_root = settings.project_root or Path.cwd()

    resolved = real_path.resolve()

    # Check /memories/ route: ~/.nova/{agent_id}/
    aid = agent_id or MAIN_AGENT_ID
    agent_dir = settings.get_agent_dir(aid)
    try:
        if resolved.is_relative_to(agent_dir.resolve()):
            relative = resolved.relative_to(agent_dir.resolve())
            return f"/memories/{relative}"
    except (ValueError, OSError):
        pass

    # Check /.nova/plans/ route: {workspace_root}/.nova/plans/
    plans_dir = workspace_root / ".nova" / "plans"
    try:
        if resolved.is_relative_to(plans_dir.resolve()):
            relative = resolved.relative_to(plans_dir.resolve())
            return f"/.nova/plans/{relative}"
    except (ValueError, OSError):
        pass

    # Check /project-memory/ route: {workspace_root}/.nova/
    nova_dir = workspace_root / ".nova"
    try:
        if resolved.is_relative_to(nova_dir.resolve()):
            relative = resolved.relative_to(nova_dir.resolve())
            return f"/project-memory/{relative}"
    except (ValueError, OSError):
        pass

    # Check workspace root (default route)
    try:
        if resolved.is_relative_to(workspace_root.resolve()):
            relative = resolved.relative_to(workspace_root.resolve())
            return f"/{relative}"
    except (ValueError, OSError):
        pass

    return None


def find_latest_plan_file_virtual(
    backend: BackendProtocol,
    plans_virtual_dir: str = "/.nova/plans/",
) -> str | None:
    """Find the most recently modified plan file using the backend.

    Uses backend.ls() to list plan files and returns the virtual path
    of the most recently modified one.

    Args:
        backend: CompositeBackend or other BackendProtocol with ls() support.
        plans_virtual_dir: Virtual path to the plans directory.

    Returns:
        Virtual path to the latest plan file (e.g., "/.nova/plans/plan-refactor.md"),
        or None if no plan files exist.
    """
    try:
        result = backend.ls(plans_virtual_dir)
        if not result:
            return None

        # Parse ls results to find plan*.md files.
        # The ls result format depends on the backend implementation.
        plan_files: list[tuple[str, float]] = []

        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    name = item.get("name", item.get("path", ""))
                    mtime = item.get("mtime", item.get("modified", 0))
                    if name.endswith(".md") and (name.startswith("plan") or name == "plan.md"):
                        path = item.get("path", f"{plans_virtual_dir}{name}")
                        plan_files.append((path, mtime))
                elif isinstance(item, str):
                    if item.endswith(".md") and (item.startswith("plan") or item == "plan.md"):
                        plan_files.append((f"{plans_virtual_dir}{item}", 0))
        elif isinstance(result, str):
            # Some backends return a formatted string.
            for line in result.strip().splitlines():
                name = line.strip().split()[-1] if line.strip() else ""
                if name.endswith(".md") and (name.startswith("plan") or name == "plan.md"):
                    plan_files.append((f"{plans_virtual_dir}{name}", 0))

        if not plan_files:
            return None

        # Return the most recently modified plan file.
        plan_files.sort(key=lambda x: x[1], reverse=True)
        return plan_files[0][0]

    except Exception:
        logger.debug("Failed to list plan files via backend", exc_info=True)
        return None


def read_via_backend(
    virtual_path: str,
    backend: BackendProtocol,
) -> str | None:
    """Read file content through the backend using a virtual path.

    Args:
        virtual_path: Virtual path (e.g., "/memories/agent.md").
        backend: BackendProtocol with read() support.

    Returns:
        File content as string, or None if the file doesn't exist or
        can't be read.
    """
    try:
        result = backend.read(virtual_path)
        if result is None:
            return None
        if isinstance(result, str):
            # Check for error responses.
            if result.startswith("Error:") or result.startswith("error:"):
                return None
            return result
        # Some backends return ReadResult objects (content attribute).
        if hasattr(result, "content"):
            return result.content
        # FilesystemBackend returns ReadResult with file_data (FileData wrapper).
        if hasattr(result, "file_data"):
            # Check for error responses first.
            if hasattr(result, "error") and result.error:
                return None
            if result.file_data is not None:
                return result.file_data.get("content", "")
        return str(result)
    except Exception:
        logger.debug("Failed to read %s via backend", virtual_path, exc_info=True)
        return None


async def aread_via_backend(
    virtual_path: str,
    backend: BackendProtocol,
) -> str | None:
    """Async counterpart of :func:`read_via_backend`.

    Uses ``backend.aread`` so callers inside a running event loop (e.g. a
    middleware's ``abefore_agent`` during ``ainvoke``) never trip the sync
    sandbox-execute deadlock — a sandbox's sync ``read`` bridges to async via
    ``run_coroutine_threadsafe`` on the *running* loop, which blocks forever.

    Args:
        virtual_path: Virtual path (e.g., "/memories/agent.md").
        backend: BackendProtocol with aread() support.

    Returns:
        File content as string, or None if the file doesn't exist or can't be read.
    """
    try:
        result = await backend.aread(virtual_path)
        if result is None:
            return None
        if isinstance(result, str):
            if result.startswith(("Error:", "error:")):
                return None
            return result
        if hasattr(result, "content"):
            return result.content
        if hasattr(result, "file_data"):
            if hasattr(result, "error") and result.error:
                return None
            if result.file_data is not None:
                return result.file_data.get("content", "")
        return str(result)
    except Exception:  # noqa: BLE001 — best-effort read; mirror read_via_backend
        logger.debug("Failed to aread %s via backend", virtual_path, exc_info=True)
        return None


def write_via_backend(
    virtual_path: str,
    content: str,
    backend: BackendProtocol,
) -> bool:
    """Write file content through the backend using a virtual path.

    Args:
        virtual_path: Virtual path (e.g., "/memories/agent.md").
        content: File content to write.
        backend: BackendProtocol with write() support.

    Returns:
        True if write succeeded, False otherwise.
    """
    try:
        result = backend.write(virtual_path, content)
        if result is None:
            return True  # Some backends return None on success.
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            return result.get("success", False)
        # WriteResult objects.
        if hasattr(result, "success"):
            return result.success
        return True
    except Exception:
        logger.debug("Failed to write %s via backend", virtual_path, exc_info=True)
        return False


def _get_routes(backend: BackendProtocol) -> list[tuple[str, BackendProtocol]]:
    """Extract route prefix/backend pairs from a CompositeBackend.

    Args:
        backend: A CompositeBackend instance.

    Returns:
        List of (prefix, sub_backend) tuples, sorted by prefix length
        (longest first for correct matching).
    """
    routes: list[tuple[str, BackendProtocol]] = []
    if hasattr(backend, "routes") and isinstance(backend.routes, dict):
        for prefix, sub_backend in backend.routes.items():
            routes.append((prefix, sub_backend))
    # Sort by prefix length (longest first) for correct matching.
    routes.sort(key=lambda x: len(x[0]), reverse=True)
    return routes


def resolve_memory_virtual_path(
    memory_type: str,
    path: str | None = None,
    *,
    agent_id: str | None = None,
) -> str:
    """Resolve a memory type and optional path to a virtual path.

    This is used by memory tools to convert memory_type shortcuts to
    virtual paths that can be routed through the CompositeBackend.

    Args:
        memory_type: "user" or "project".
        path: Optional custom path. If it starts with "/", it's already
            a virtual path and is returned as-is.
        agent_id: Agent identifier for user memory.

    Returns:
        Virtual path string (e.g., "/memories/agent.md").

    Raises:
        ValueError: If memory_type is invalid and no path is provided.
    """
    if path and path.startswith("/"):
        # Already a virtual path.
        return path

    if path:
        # Custom path provided — resolve based on memory_type.
        if memory_type == "user":
            return f"/memories/{path}"
        else:
            return f"/project-memory/{path}"

    # Default paths based on memory_type.
    if memory_type == "user":
        return "/memories/agent.md"
    elif memory_type == "project":
        return "/project-memory/NOVA.md"
    else:
        raise ValueError(f"Invalid memory_type: {memory_type!r}. Use 'user' or 'project'.")

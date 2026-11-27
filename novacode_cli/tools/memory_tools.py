"""Memory management tools.

This module provides tools for persisting information across sessions.
All tools support virtual paths that route through the CompositeBackend:
    - /memories/          → user memory (~/.nova/{agent-id}/)
    - /project-memory/    → project memory ({workspace-root}/.nova/)

When a virtual path is provided, it is resolved to a real filesystem path
for the actual file operation. The response always returns the virtual path
so the LLM sees consistent path formatting regardless of the OS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain.tools import tool
from pydantic import WithJsonSchema

#: ``memory_type`` accepts ``"user"`` or ``"project"``. The JSON schema still
#: advertises that enum (it is what the model reads), but validation is a plain
#: ``str``: when a virtual ``path`` is supplied it alone determines the file, so
#: a guessed value like ``"topic"`` — which the model does send for a topic file
#: under ``/memories/memories/`` — must not hard-fail the whole call before the
#: tool body can ignore it. The no-path case is validated inside
#: :func:`_resolve_memory_path` and returns a helpful ``success: False`` dict.
MemoryType = Annotated[
    str, WithJsonSchema({"type": "string", "enum": ["user", "project"]})
]


def _normalize_memory_type(memory_type: str | None) -> str:
    """Lower-case/trim a memory type; unknown values pass through for validation."""
    return (memory_type or "user").strip().lower()


def _resolve_memory_path(
    memory_type: str,
    path: str | None = None,
) -> tuple[Path, str]:
    """Resolve a memory type and optional path to real and virtual paths.

    Args:
        memory_type: "user" or "project". Ignored when *path* is a virtual path
            (the path is authoritative); validated only for the no-path default.
        path: Optional custom path. If it starts with "/", it's treated as
            a virtual path and resolved to a real path. Otherwise, it's
            treated as a real filesystem path.

    Returns:
        Tuple of (real_path, virtual_path).

    Raises:
        ValueError: If memory_type is invalid (and no path was given) or path
            cannot be resolved.
    """
    from novacode_cli.config.config import MAIN_AGENT_ID, settings
    from novacode_cli.utils.backend_paths import virtual_to_real_path

    if path and path.startswith("/"):
        # Virtual path — resolve to real path. This is authoritative, so an
        # out-of-enum memory_type (e.g. the model's "topic" for a topic file)
        # is harmless and deliberately not validated here.
        real_path = virtual_to_real_path(
            path,
            agent_id=MAIN_AGENT_ID,
            workspace_root=settings.project_root,
        )
        if real_path is None:
            msg = f"Cannot resolve virtual path: {path}"
            raise ValueError(msg)
        return real_path, path

    if path:
        # Custom real path — use directly, compute virtual path.
        real_path = Path(path)
        from novacode_cli.utils.backend_paths import real_to_virtual_path

        virtual = real_to_virtual_path(
            real_path,
            agent_id=MAIN_AGENT_ID,
            workspace_root=settings.project_root,
        )
        return real_path, virtual or str(real_path)

    # Default paths based on memory_type. Validated only here — a path is the
    # only way to name a topic file, so without one the type must be exact.
    normalized = _normalize_memory_type(memory_type)
    if normalized == "user":
        agent_dir = settings.get_agent_dir(MAIN_AGENT_ID)
        real_path = agent_dir / "agent.md"
        return real_path, "/memories/agent.md"

    if normalized == "project":
        if not settings.project_root:
            msg = "Not in a project directory. Use memory_type='user' for user memory."
            raise ValueError(msg)
        real_path = settings.project_root / ".nova" / "NOVA.md"
        return real_path, "/project-memory/NOVA.md"

    msg = (
        f"Invalid memory_type: {memory_type!r}. Use 'user' or 'project'. "
        "To read a topic file, pass its path, e.g. "
        "path='/memories/memories/<topic>.md'."
    )
    raise ValueError(msg)


@tool
def write_memory(
    content: str,
    memory_type: MemoryType = "user",
    path: str | None = None,
    append: bool = False,
) -> dict[str, Any]:
    r"""Write content to agent memory file for persistence across sessions.

    Memory allows the agent to remember information across conversations. Use
    this tool proactively — not only when the user asks — whenever durable,
    memory-worthy information emerges: user preferences and corrections,
    decisions with rationale, verified debugging fixes, non-obvious commands,
    version constraints and environment gotchas. Do not end a session with
    learned-but-unrecorded durable facts. Skip trivia, transient task status
    and duplicates.

    For discrete, look-up-by-key facts shared across subagents, prefer the
    ``remember`` / ``recall`` tools (durable LangGraph store). Use this tool for
    prose memory that should be injected into the system prompt each session.

    Note: this writes to the **local** filesystem (~/.nova for user memory,
    <project>/.nova for project memory). In a bind-mounted Docker sandbox those
    files are visible inside the container, but in a remote/cloud sandbox the
    agent reads project memory from the sandbox — so a project-memory write here
    may not be reflected in the sandbox view within the same run.

    Args:
        content: Memory content to write (Markdown format recommended)
        memory_type: Must be "user" or "project" — "user" for user preferences
                    (applies to all projects), "project" for project-specific
                    context. Ignored when ``path`` is a virtual path.
        path: Optional custom path. Supports virtual paths:
            - /memories/agent.md (user memory)
            - /memories/memories/preferences.md (advanced structure)
            - /project-memory/NOVA.md (project memory)
            - /project-memory/custom.md (custom project memory)
            If not provided, defaults to standard locations based on memory_type.
        append: If True, append to existing memory; if False, replace (default: False)

    Returns:
        Dictionary with:
        - success: bool - Whether write succeeded
        - path: str - Virtual path to memory file (e.g., /memories/agent.md)
        - message: str - Success/error message
        - memory_type: str - Type of memory written

    Memory Locations (virtual paths):
        - User memory: /memories/agent.md
        - Project memory: /project-memory/NOVA.md

    Example:
        >>> write_memory("# Preferences\n\n- Use concise responses", memory_type="user")
        {'success': True, 'path': '/memories/agent.md', 'message': '...'}

        >>> write_memory("# Project Notes\n\n- Use Python 3.11+", memory_type="project")
        {'success': True, 'path': '/project-memory/NOVA.md', 'message': '...'}
    """

    # Resolve memory path (both real and virtual).
    memory_type = _normalize_memory_type(memory_type)
    try:
        memory_path, virtual_path = _resolve_memory_path(memory_type, path)
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "memory_type": memory_type,
        }

    from novacode_cli.config.config import MAIN_AGENT_ID, settings
    from novacode_cli.hermes.memory_tiers import PROJECTS_DIRNAME, project_memory_key

    agent_dir = settings.get_agent_dir(MAIN_AGENT_ID)
    projects_dir = (agent_dir / "memories" / PROJECTS_DIRNAME).resolve()
    try:
        flat_target = memory_path.resolve().parent == projects_dir
    except OSError:
        flat_target = False
    if flat_target:
        # Flat pages under projects/ are invisible to the wiki, /dream and
        # memory_search, which all read projects/<project-key>/<topic>.md.
        # Stranded pages are exactly how the wiki stops accumulating.
        key = project_memory_key(settings.project_root)
        scoped = f"/memories/memories/{PROJECTS_DIRNAME}/{key}/{memory_path.name}"
        return {
            "success": False,
            "error": (
                "Project memory pages live under projects/<project-key>/, never "
                f"flat inside projects/. Write to {scoped} instead (this "
                f"session's project key is {key or 'unavailable'}; another "
                "project's page belongs in that project's key directory)."
            ),
            "memory_type": memory_type,
        }

    # Create parent directory if needed
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content if appending. Mirror read_memory's encoding
    # fallback (UTF-8 → cp1252 → UTF-8 w/ replacement) so a memory file that
    # was written with a Windows-default encoding doesn't make append crash.
    existing_content = ""
    if append and memory_path.exists():
        try:
            existing_content = memory_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                existing_content = memory_path.read_text(encoding="cp1252")
            except (UnicodeDecodeError, OSError):
                existing_content = memory_path.read_text(
                    encoding="utf-8", errors="replace"
                )
        except OSError as e:
            return {
                "success": False,
                "error": (f"Failed to read existing memory: {e!s}"),
                "path": virtual_path,
                "memory_type": memory_type,
            }

    # Write or append content
    try:
        if append and existing_content:
            # Append with separator
            full_content = f"{existing_content}\n\n---\n\n{content}"
        else:
            full_content = content

        memory_path.write_text(full_content, encoding="utf-8")

        return {
            "success": True,
            "path": virtual_path,
            "message": f"Memory written to {virtual_path}",
            "memory_type": memory_type,
            "appended": append,
            "content_length": len(full_content),
        }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"Failed to write memory: {e!s}",
            "path": virtual_path,
            "memory_type": memory_type,
        }


@tool
def read_memory(
    memory_type: MemoryType = "user",
    path: str | None = None,
) -> dict[str, Any]:
    r"""Read agent memory file to see what the agent remembers.

    Use this tool to check what information is stored in memory before
    updating it or when the user asks "what do you remember?"

    Args:
        memory_type: Must be "user" or "project" — "user" for user preferences,
                    "project" for project context. Ignored when ``path`` is a
                    virtual path (use "user" to read any ``/memories/`` file,
                    including a topic file under ``/memories/memories/``).
        path: Optional custom path. Supports virtual paths:
            - /memories/agent.md (user memory)
            - /memories/memories/preferences.md (advanced structure)
            - /project-memory/NOVA.md (project memory)
            If not provided, defaults to standard locations based on memory_type.

    Returns:
        Dictionary with:
        - success: bool - Whether read succeeded
        - content: str - Memory file content
        - path: str - Virtual path to memory file (e.g., /memories/agent.md)
        - exists: bool - Whether memory file exists
        - memory_type: str - Type of memory read

    Example:
        >>> read_memory(memory_type="user")
        {'success': True, 'content': '# Preferences\n\n...', 'path': '/memories/agent.md'}
    """

    # Resolve memory path (both real and virtual).
    memory_type = _normalize_memory_type(memory_type)
    try:
        memory_path, virtual_path = _resolve_memory_path(memory_type, path)
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "memory_type": memory_type,
        }

    # Check if memory exists
    if not memory_path.exists():
        return {
            "success": True,
            "content": "",
            "path": virtual_path,
            "exists": False,
            "message": f"No memory file found at {virtual_path}",
            "memory_type": memory_type,
        }

    # Read memory — try UTF-8 first, then cp1252 (Windows default),
    # then UTF-8 with replacement as a last resort.
    try:
        content = memory_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = memory_path.read_text(encoding="cp1252")
        except (UnicodeDecodeError, OSError):
            content = memory_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "success": False,
            "error": f"Failed to read memory: file not accessible",
            "path": virtual_path,
            "memory_type": memory_type,
        }

    return {
        "success": True,
        "content": content,
        "path": virtual_path,
        "exists": True,
        "content_length": len(content),
        "memory_type": memory_type,
    }



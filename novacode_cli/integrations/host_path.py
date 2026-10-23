"""Host-path → virtual-path normalization for filesystem tools.

## Why this exists

Nova's agents are told to address files with **virtual paths starting with
``/``** rooted at the project (``/novacode_cli/x`` → ``<project>/novacode_cli/x``).
But the model constantly *sees* real host paths — in the IDE's open-file context,
in ``@file`` mentions, in prior tool output, in error traces — and so it very
often passes a real **host absolute path** to a file tool, e.g.::

    read_file("B:/Summer Project 2026/Nova-Code/nova-code-cli/novacode_cli/prompts/plan_agent.jinja")

deepagents' ``validate_path`` (called by ``FilesystemMiddleware`` before the
backend) then rejects it hard::

    Error: Windows absolute paths are not supported: B:/… Please use virtual
    paths starting with / (e.g., /workspace/file.txt)

Telling the agent "don't do that" in the prompt isn't enough — it slips up
constantly. :func:`host_path_to_virtual` rewrites any host absolute path **that
points inside the project** into the equivalent ``/``-rooted virtual path, so the
mistake becomes harmless. It is applied by monkey-patching ``validate_path`` (see
:func:`novacode_cli.utils.backend_patches.apply_filesystem_host_path_patch`).

Paths that are already virtual, relative, or outside the project are returned
unchanged, so genuinely-invalid paths still surface deepagents' helpful error.
Handles Windows (``B:\\…`` / ``B:/…``, case-insensitive) and POSIX
(``/home/user/project/…``) host paths.
"""

from __future__ import annotations

import os
import posixpath


def _relative_under(norm_path: str, host_root: str) -> str | None:
    """Return the posix relative path if ``norm_path`` is at/under ``host_root``.

    ``""`` means the path *is* the root; ``None`` means it's outside. Comparison
    is case-insensitive on Windows.
    """
    root = str(host_root).replace("\\", "/").rstrip("/")
    if not root:
        return None
    ci = os.name == "nt"
    a, b = (norm_path.lower(), root.lower()) if ci else (norm_path, root)
    if a == b:
        return ""
    if a.startswith(b + "/"):
        return norm_path[len(root):].lstrip("/")  # keep original casing of the rest
    return None


def host_path_to_virtual(
    path: str,
    workspace_root: str,
    mounts: list[tuple[str, str]] | None = None,
) -> str:
    """Rewrite a host absolute path to the virtual path the backend serves it at.

    Handles the project root *and* extra mounted roots (e.g. the skills dirs,
    which are mounted at ``/skills/`` and ``/claude-skills/`` outside the
    project) so the agent can pass either a host path or a virtual one.

    Args:
        path: The path the agent passed to a file tool.
        workspace_root: The project root on the host, served at ``/``.
        mounts: Extra ``(host_root, virtual_prefix)`` pairs, e.g.
            ``[("C:/Users/x/.nova/skills", "/skills/")]``.

    Returns:
        The ``/``-rooted virtual path when ``path`` is at/under a known root;
        otherwise ``path`` unchanged (already virtual/relative, or genuinely
        outside every root).
    """
    if not isinstance(path, str) or not path:
        return path

    norm = path.replace("\\", "/")
    # Never fold traversal across a route boundary. The filesystem validator
    # must see and reject it, rather than receive a different virtual target.
    if ".." in norm.split("/") or "\x00" in norm:
        return path

    # Mounted roots first — they're more specific than the project root.
    for host_root, prefix in mounts or []:
        rel = _relative_under(norm, host_root)
        if rel is not None:
            base = "/" + prefix.strip("/")
            return f"{base}/{posixpath.normpath(rel)}" if rel else f"{base}/"

    if workspace_root:
        rel = _relative_under(norm, workspace_root)
        if rel is not None:
            return "/" + posixpath.normpath(rel) if rel else "/"
    return path


__all__ = ["host_path_to_virtual"]


if __name__ == "__main__":
    # ponytail: self-check for the mapping (project root + skills mount).
    ws = "C:/proj"
    m = [("C:/Users/x/.nova/skills", "/skills/")]
    assert host_path_to_virtual(r"C:\proj\a\b.py", ws) == "/a/b.py"
    assert host_path_to_virtual(r"C:\Users\x\.nova\skills\stop-slop\SKILL.md", ws, m) == "/skills/stop-slop/SKILL.md"
    assert host_path_to_virtual("/already/virtual", ws, m) == "/already/virtual"
    assert host_path_to_virtual(r"C:\other\file.txt", ws, m) == r"C:\other\file.txt"  # outside → unchanged
    print("host_path self-check ok")

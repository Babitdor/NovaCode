"""Resolve file-tool destinations before the parent approves a teammate call."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from novacode_cli.security.policy import ApprovalPolicy


class RoutedBackend(Protocol):
    """Only the routing method needed to identify a mounted file destination."""

    def _get_backend_and_key(self, key: str) -> tuple[Any, str]: ...


WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "str_replace",
        "delete_file",
        "NotebookEditTool",
    }
)


def resolve_write_paths(
    name: str,
    args: dict[str, Any],
    workspace: Path | None,
    backend: RoutedBackend | None = None,
) -> list[Path]:
    """Mirror virtual filesystem routes, resolving symlinks without writing.

    Leading slash paths are virtual unless they are real workspace absolute
    paths or an explicit host-drive route. Non-filesystem stores fail closed.
    """
    if name not in WRITE_TOOLS:
        return []
    raw = args.get("file_path")
    if not isinstance(raw, str) or not raw.strip():
        message = "Missing file path for team write"
        raise ValueError(message)
    workspace = (workspace or Path.cwd()).resolve()
    path = Path(raw)
    if path.is_absolute() and path.is_relative_to(workspace):
        return [path.resolve()]
    if backend is not None:
        selected, key = backend._get_backend_and_key(raw)
        resolver = getattr(selected, "_resolve_path", None)
        if not callable(resolver):
            message = "Cannot resolve file path for team write"
            raise ValueError(message)
        return [Path(resolver(key)).resolve()]
    if path.is_absolute() and path.drive:
        return [path.resolve()]
    return [(workspace / raw.lstrip("/\\")).resolve()]


def write_boundary(
    name: str,
    args: dict[str, Any],
    worker_root: Path,
    supplied: object,
    policy: ApprovalPolicy,
) -> tuple[list[Path], bool]:
    """Evaluate canonical destinations on the parent, off its event loop."""
    from novacode_cli.security.policy import _glob_match

    root = worker_root.resolve()
    destinations = resolve_write_paths(name, args, root)
    if destinations and supplied is not None:
        if not isinstance(supplied, list) or len(supplied) != len(destinations):
            message = "Invalid resolved write paths"
            raise ValueError(message)
        if not all(isinstance(path, str) and Path(path).is_absolute() for path in supplied):
            message = "Resolved write paths must be absolute"
            raise ValueError(message)
        destinations = [Path(path).resolve() for path in supplied]
    outside = False
    for path in destinations:
        verdict = policy.evaluate(name, {**args, "file_path": str(path)})
        if verdict.tier == "deny":
            message = "Resolved path denied by parent approval policy"
            raise ValueError(message)
        explicitly_allowed = any(
            _glob_match(path.as_posix(), rule.replace("\\", "/")) for rule in policy.path_allow
        )
        if not path.is_relative_to(root) and not explicitly_allowed:
            outside = True
    return destinations, outside

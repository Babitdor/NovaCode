"""Portable validation for paths inside a virtual filesystem root."""

from __future__ import annotations

import ntpath
import os
from pathlib import Path, PureWindowsPath


def virtual_relative_path(key: str) -> str:
    """Strip one virtual root without accepting host anchors or parent traversal."""
    normalized = key.replace("\\", "/")
    relative = normalized.removeprefix("/")
    if (
        "\x00" in normalized
        or normalized.startswith("//")
        or ntpath.splitdrive(relative)[0]
        or ".." in relative.split("/")
        or relative.startswith("~")
    ):
        raise ValueError("Path traversal or host anchor not allowed in virtual path")
    if os.name == "nt" and any(
        ":" in part or PureWindowsPath(part).is_reserved() for part in relative.split("/") if part
    ):
        raise ValueError("Windows device names and alternate data streams are not allowed")
    return relative


def resolve_beneath(root: Path, key: str) -> Path:
    """Resolve a virtual key and refuse symlink/junction escapes from its route."""
    root = root.resolve()
    target = (root / virtual_relative_path(key)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Path resolves outside its virtual root")
    return target

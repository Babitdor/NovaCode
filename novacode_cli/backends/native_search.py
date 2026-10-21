"""Optional native search accelerators with shell-free, bounded subprocesses."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def bfs_files(root: Path, skip_dirs: frozenset[str], timeout: int = 20) -> list[Path] | None:
    """Return regular files, or None to request the existing Python walker."""
    executable = shutil.which("bfs")
    if executable is None:
        return None
    command = [executable, "."]
    for name in sorted(skip_dirs | {"*.egg-info"}):
        command += ["-exclude", "(", "-type", "d", "-name", name, ")"]
    command += ["-type", "f", "-print0"]
    try:
        result = subprocess.run(
            command, cwd=root, capture_output=True, timeout=timeout, check=False
        )
        if result.returncode != 0:
            return None
        paths = [
            root / Path(part.decode("utf-8", errors="surrogateescape"))
            for part in (result.stdout or b"").split(b"\0")
            if part
        ]
        return [p for p in paths if p.resolve().is_relative_to(root.resolve())]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def ugrep_search(
    pattern: str,
    base: Path,
    include_glob: str | None,
    *,
    use_regex: bool,
    skip_dirs: frozenset[str],
    timeout: int = 30,
) -> dict[str, list[tuple[int, str]]] | None:
    """Normalize ugrep JSON into paths and line matches; None means fallback."""
    executable = shutil.which("ugrep")
    if executable is None:
        return None
    # ugrep (not the configurable `ug` wrapper) has predictable default options.
    command = [executable, "--json", "-n", "-H", "-r", "-I", "-j", "--ignore-files"]
    if not use_regex:
        command.append("-F")
    for name in sorted(skip_dirs | {"*.egg-info"}):
        command.append(f"--exclude-dir={name}")
    if include_glob:
        command += ["-g", include_glob]
    command += ["--", pattern, "." if base.is_dir() else base.name]
    root = base if base.is_dir() else base.parent
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode not in (0, 1):
            return None
        if result.returncode == 1:
            return {}
        records = json.loads(result.stdout or "[]")
        if not isinstance(records, list):
            return None
        matches: dict[str, list[tuple[int, str]]] = {}
        for record in records:
            path = (root / record["file"]).resolve()
            if not path.is_relative_to(root.resolve()) or (
                base.is_file() and path != base.resolve()
            ):
                continue
            for match in record["matches"]:
                if type(match["line"]) is not int or not isinstance(match["match"], str):
                    return None
                matches.setdefault(str(path), []).append(
                    (match["line"], match["match"].rstrip("\r\n"))
                )
        return matches
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None

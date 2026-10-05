"""Run the official Skills CLI without reimplementing its install workflow."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def add_arguments(argv: list[str]) -> list[str] | None:
    """Return untouched add arguments, including Nova's natural-language alias."""
    if argv[:2] == ["skills", "add"] or argv[:2] == ["add", "skill"]:
        return argv[2:]
    return None


def skills_command() -> list[str]:
    """Find npx, invoking its JS entry point on Windows to avoid cmd parsing."""
    executable = shutil.which("npx")
    if executable is None:
        msg = "Node.js and npx are required. Install Node.js, then retry nova skills add."
        raise FileNotFoundError(msg)
    path = Path(executable)
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        entry = path.parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        node = shutil.which("node")
        if node is None or not entry.is_file():
            msg = "Cannot locate Node.js's npx-cli.js. Repair your Node.js/npm installation."
            raise FileNotFoundError(msg)
        return [node, str(entry), "--yes", "skills@latest"]
    return [executable, "--yes", "skills@latest"]


def run_skills_cli(command: str, arguments: list[str]) -> int:
    """Inherit the terminal and credentials, and propagate the upstream exit code."""
    try:
        result = subprocess.run(  # noqa: S603 - argv only, no shell interpolation
            [*skills_command(), command, *arguments],
            check=False,
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 127
    except OSError as exc:
        sys.stderr.write(f"Could not start the Skills CLI: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return 130
    return result.returncode if result.returncode >= 0 else 128 - result.returncode

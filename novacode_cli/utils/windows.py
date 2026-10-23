"""Trusted Windows command locations; never search an agent's project or PATH."""

import os
from pathlib import Path


def taskkill_executable() -> str:
    """Use the operating system copy rather than a same-named project program."""
    return str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe")

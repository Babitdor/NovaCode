"""The command that adds an optional package to the Nova that is running.

``novacode-cli`` is not on PyPI, so a hint such as ``uv tool install --with x
novacode-cli`` cannot resolve, and ``uv pip install -e '.[voice]'`` only works
from inside the repository. The right command depends on how this Nova was
installed:

* **uv tool** (an ``uv-receipt.toml`` beside the environment): reinstall from the
  source the receipt records (an editable checkout, a Git URL, a directory) with
  the extra added to the extras it already has, so ``uv tool upgrade`` keeps it.
* **anything else** (``uv run``'s ``.venv``, a pip virtualenv): install the
  packages into the interpreter that is running Nova.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

_NAME = "novacode-cli"


def _quote(value: str) -> str:
    """Quote for both cmd/PowerShell and POSIX shells when it is needed."""
    return f'"{value}"' if any(c in value for c in " []&()'<>") else value


def _receipt_requirement(prefix: Path | None = None) -> dict | None:
    """Nova's entry in the uv tool receipt, or ``None`` when not a uv tool install."""
    receipt = (prefix or Path(sys.prefix)) / "uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for requirement in data.get("tool", {}).get("requirements", []):
        if isinstance(requirement, dict) and requirement.get("name") == _NAME:
            return requirement
    return None


def install_command(
    packages: Sequence[str],
    extra: str | None = None,
    *,
    index_url: str | None = None,
    args: str = "",
    prefix: Path | None = None,
) -> str:
    """The one command that installs *packages* (Nova's *extra*, if it has one).

    Args:
        packages: Distribution names, used when installing into this interpreter.
        extra: Nova's optional-dependency group that holds them (``voice-pocket``).
        index_url: An extra package index some wheels come from.
        args: Extra installer flags valid for both uv pip and pip
            (``--force-reinstall --no-binary x``); forces the interpreter form.
        prefix: The environment to describe; the running one by default.
    """
    index = f" --extra-index-url {index_url}" if index_url else ""
    requirement = _receipt_requirement(prefix) if extra and not args else None
    if requirement is not None:
        extras = ",".join(sorted({*requirement.get("extras", []), extra}))
        if requirement.get("editable"):
            source = "-e " + _quote(f"{requirement['editable']}[{extras}]")
        elif requirement.get("directory"):
            source = _quote(f"{requirement['directory']}[{extras}]")
        elif requirement.get("git"):
            source = _quote(f"{_NAME}[{extras}] @ git+{requirement['git']}")
        elif requirement.get("url"):
            source = _quote(f"{_NAME}[{extras}] @ {requirement['url']}")
        else:
            source = _quote(f"{_NAME}[{extras}]")
        return f"uv tool install --reinstall {source}{index}"
    interpreter = ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python = _quote(str(prefix / interpreter if prefix else sys.executable))
    names = " ".join(_quote(p) for p in packages) + (f" {args}" if args else "")
    if shutil.which("uv"):
        return f"uv pip install --python {python} {names}{index}"
    return f"{python} -m pip install {names}{index}"


def missing_package(package: str) -> str:
    """For a package Nova always installs, missing from a damaged environment."""
    return f"{package} is not installed. Install it with: {install_command([package])}"

"""Show the project's own lint and type errors right after a file edit.

Without this the model only sees a broken edit if it decides to run the checks
itself, usually several steps later. After a successful ``write_file`` or
``edit_file`` on a local project file, the checkers the project has configured
run on that file and their errors are appended to the tool result:

* ``.py``/``.pyi``: ruff (``ruff.toml``, ``.ruff.toml`` or ``[tool.ruff]``) and
  pyright (``pyrightconfig.json`` or ``[tool.pyright]``).
* ``.ts``/``.tsx``/``.mts``/``.cts``: ``tsc --noEmit`` for the nearest
  ``tsconfig.json``, filtered to the edited file.

A checker is used only when its config is found between the file and the
workspace root and its executable is on PATH outside the workspace. The
project's own ``.venv`` and ``node_modules/.bin`` copies are repository code, so
they run only with ``"edit_diagnostics_project_tools": true``. Clean runs,
timeouts (which kill the checker's whole process tree) and missing tools add
nothing. Problems near the edited lines come first, so a new error deep in a file
with many existing ones survives the size cap; what the cap cuts is counted.
Disable with ``"edit_diagnostics_enabled": false`` in Nova.config.json.
"""
# ruff: noqa: INP001, ANN401 -- existing namespace and heterogeneous LangGraph results.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest

logger = logging.getLogger(__name__)

EDIT_TOOLS = frozenset({"write_file", "edit_file"})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
TS_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})
# ponytail: per-checker timeout; tsc type-checks the whole project, so a large
# one can exceed this and report nothing. Raise it, or switch to tsc --build, if that matters.
TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 3000
# A problem this many lines from the edited text still counts as near it.
NEAR_LINES = 3
_LOCATION = re.compile(r"[:(](\d+)[:,]\d+\b")
_CONCISE = ("--output-format", "concise")


def _dirs(path: Path, root: Path) -> list[Path]:
    """The file's directory and each parent up to and including the workspace root."""
    return [d for d in path.parents if d == root or d.is_relative_to(root)]


def _pyproject_has(directory: Path, table: str) -> bool:
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return table in data.get("tool", {})


def _config_dir(dirs: list[Path], files: tuple[str, ...], table: str) -> Path | None:
    """The nearest directory configuring a checker, from the file up to the root."""
    return next(
        (d for d in dirs if any((d / f).is_file() for f in files) or _pyproject_has(d, table)),
        None,
    )


def _inside(found: str, root: Path) -> bool:
    try:
        return Path(found).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return True  # unresolvable: treat as untrusted


def _executable(root: Path, name: str, *, project_tools: bool) -> str | None:
    """A checker on PATH; the project's own ``.venv``/``node_modules`` copy only when opted in.

    A repository can ship its own ``ruff`` or ``tsc``. Running it after every edit
    would execute repository code without approval and outside the shell sandbox,
    so by default any executable inside the workspace is refused, including one
    Windows finds in the current directory before PATH.
    """
    if project_tools:
        for local in (
            root / ".venv" / "Scripts",
            root / ".venv" / "bin",
            root / "node_modules" / ".bin",
        ):
            if local.is_dir() and (found := shutil.which(name, path=str(local))):
                return found
    found = shutil.which(name)
    if found and not project_tools and _inside(found, root):
        return None
    return found


def checkers(path: Path, root: Path, *, project_tools: bool = False) -> list[tuple[str, list[str]]]:
    """``(label, argv)`` for each checker this project configures for ``path``."""
    dirs = _dirs(path, root)
    found: list[tuple[str, list[str]]] = []
    if path.suffix in PYTHON_SUFFIXES:
        ruff = _executable(root, "ruff", project_tools=project_tools)
        if ruff and _config_dir(dirs, ("ruff.toml", ".ruff.toml"), "ruff"):
            found.append(
                (
                    "ruff",
                    # --force-exclude keeps the project's exclude list for an explicit path.
                    [ruff, "check", "--no-fix", "--force-exclude", *_CONCISE, str(path)],
                )
            )
        pyright = _executable(root, "pyright", project_tools=project_tools)
        config = _config_dir(dirs, ("pyrightconfig.json",), "pyright")
        if pyright and config:
            # -p: the nearest config (its venv, extraPaths), not the root's defaults.
            found.append(("pyright", [pyright, "-p", str(config), str(path)]))
    elif path.suffix in TS_SUFFIXES:
        tsconfig = next(
            (d / "tsconfig.json" for d in dirs if (d / "tsconfig.json").is_file()), None
        )
        tsc = _executable(root, "tsc", project_tools=project_tools)
        if tsc and tsconfig:
            found.append(("tsc", [tsc, "--noEmit", "--pretty", "false", "-p", str(tsconfig)]))
    return found


def _only_file(output: str, path: Path, root: Path) -> str:
    """Keep the edited file's errors and detail lines from tsc's whole-project report."""
    names = {path.relative_to(root).as_posix(), path.as_posix(), str(path)}
    kept: list[str] = []
    keep = False
    for line in output.splitlines():
        if line[:1].isspace():
            if keep:
                kept.append(line)
            continue
        # tsc prints "path(line,col): ...": match the whole path, not a prefix
        # of src/a.tsx or a suffix of lib/src/a.ts.
        keep = any(line.startswith(name + "(") for name in names)
        if keep:
            kept.append(line)
    return "\n".join(kept)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the checker and its children: an npm ``.cmd`` shim's node child holds the pipes."""
    try:
        if sys.platform == "win32":
            from novacode_cli.utils.windows import taskkill_executable

            subprocess.run(  # noqa: S603 -- fixed taskkill arguments.
                [taskkill_executable(), "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def _run(label: str, argv: list[str], path: Path, root: Path) -> str:
    group = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a resolved checker and a project path.
            argv,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group,
        )
    except OSError:
        logger.debug("Edit diagnostics: %s could not start", label, exc_info=True)
        return ""
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.debug("Edit diagnostics: %s timed out", label)
        _kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.communicate(timeout=5)
        return ""
    if proc.returncode == 0:
        return ""
    output = (stdout + stderr).strip()
    return _only_file(output, path, root) if label == "tsc" else output


def edited_lines(args: dict[str, Any], path: Path) -> list[tuple[int, int]]:
    """1-based ``(first, last)`` line ranges where ``new_string`` landed, else ``[]``.

    ``write_file`` and deletions have no ``new_string`` to find, and an
    indent-shifted fallback edit no longer matches it verbatim; those keep the
    checker's own line order.
    """
    new = args.get("new_string")
    if not isinstance(new, str) or not new.strip():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ranges = []
    start = 0
    while (index := text.find(new, start)) != -1:
        first = text.count("\n", 0, index) + 1
        ranges.append((first, first + new.count("\n")))
        if not args.get("replace_all"):
            break
        start = index + len(new)
    return ranges


def _records(output: str) -> tuple[list[tuple[int, str]], list[str]]:
    """``(line, text)`` per reported problem, with its indented detail lines; and the rest.

    ruff ``file:12:5:``, pyright ``file:12:5 -`` and tsc ``file(12,5):`` all put
    the line number first after the path.
    """
    records: list[tuple[int, str]] = []
    other: list[str] = []
    for line in output.splitlines():
        if match := _LOCATION.search(line):
            records.append((int(match.group(1)), line))
        elif records and line[:1].isspace():
            records[-1] = (records[-1][0], records[-1][1] + "\n" + line)
        else:
            other.append(line)
    return records, other


def prioritized(output: str, ranges: list[tuple[int, int]]) -> str:
    """Problems near the edit first, then the rest, within the size cap; count what is cut."""
    records, other = _records(output)
    near = [
        i
        for i, (line, _text) in enumerate(records)
        if any(first - NEAR_LINES <= line <= last + NEAR_LINES for first, last in ranges)
    ]
    order = near + sorted(set(range(len(records))) - set(near))
    kept: list[str] = ["Near your edit:"] if near else []
    size = sum(len(part) + 1 for part in kept)
    shown = 0
    for n, i in enumerate(order):
        part = records[i][1]
        if near and n == len(near):
            part = "Elsewhere in the file:\n" + part
        if size + len(part) + 1 > MAX_OUTPUT_CHARS:
            break
        kept.append(part)
        size += len(part) + 1
        shown += 1
    if shown < len(records):
        kept.append(f"({len(records) - shown} more problems in this file not shown)")
    else:
        kept.extend(line for line in other if line.strip())
    return "\n".join(kept)


def _resolve(file_path: str, root: Path) -> Path | None:
    """Map the agent's virtual ``/``-rooted path to a project file, or None."""
    for candidate in (root / file_path.lstrip("/\\"), Path(file_path)):
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved
    return None


def diagnostics(args: dict[str, Any], root: Path) -> str:
    """Text to append to an edit's tool result: checker errors, or ``""``."""
    config = NovaConfig()
    if not config.get_edit_diagnostics_enabled():
        return ""
    file_path = args.get("file_path") or args.get("path")
    path = _resolve(file_path, root) if isinstance(file_path, str) and file_path else None
    if path is None:
        return ""
    ranges = edited_lines(args, path)
    project_tools = config.get_edit_diagnostics_project_tools()
    reports = [
        f"{label}:\n{prioritized(output, ranges)}"
        for label, argv in checkers(path, root, project_tools=project_tools)
        if (output := _run(label, argv, path, root))
    ]
    if not reports:
        return ""
    return (
        "\n\nThe project's checkers report problems in this file after the edit:\n"
        + "\n\n".join(reports)
        + "\nFix what your change introduced; leave pre-existing problems unless asked."
    )


def _with_report(result: Any, report: str) -> Any:
    return result.model_copy(update={"content": result.content + report}) if report else result


def _eligible(request: ToolCallRequest, result: Any) -> bool:
    return (
        request.tool_call["name"] in EDIT_TOOLS
        and isinstance(result, ToolMessage)
        and result.status != "error"
        and isinstance(result.content, str)
        and not re.match(r"\s*Error\b", result.content)
    )


class EditDiagnosticsMiddleware(AgentMiddleware):
    """Append the project's lint/type errors to successful local file edits."""

    def __init__(self, workspace_root: Path | str) -> None:
        """Check files inside ``workspace_root`` only."""
        super().__init__()
        self._root = Path(workspace_root).resolve()

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """Run the checkers after a synchronous edit."""
        result = handler(request)
        if not _eligible(request, result):
            return result
        return _with_report(result, diagnostics(request.tool_call["args"], self._root))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        """Run the checkers off the event loop after an edit."""
        result = await handler(request)
        if not _eligible(request, result):
            return result
        report = await asyncio.to_thread(diagnostics, request.tool_call["args"], self._root)
        return _with_report(result, report)

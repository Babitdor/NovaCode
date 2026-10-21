"""Cell-level editing of local nbformat 4 notebooks; never executes code."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Literal

from langchain.tools import tool
from pydantic import BaseModel, ConfigDict, Field

_EDIT_LOCK = threading.Lock()
_CELL_TYPES = {"code", "markdown", "raw"}


class NotebookEditInput(BaseModel):
    """Arguments for a single notebook cell edit."""

    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str = Field(description="Existing .ipynb path in the local workspace.")
    new_source: str | None = Field(
        default=None, description="Required for replace/insert; empty text is allowed."
    )
    edit_mode: Literal["replace", "insert", "delete"] = "replace"
    cell_index: int | None = Field(default=None, ge=0, description="Zero-based cell index.")
    cell_id: str | None = Field(default=None, description="Existing cell ID; use instead of index.")
    cell_type: Literal["code", "markdown", "raw"] | None = Field(
        default=None, description="Required for insert; omitted on replace to retain type."
    )


def resolve_notebook_path(file_path: str, root: Path) -> Path:
    """Resolve workspace-relative, virtual-root, or workspace-absolute paths."""
    if not file_path.strip():
        raise ValueError("file_path must not be blank.")
    root = root.resolve()
    path = Path(file_path)
    if path.is_absolute() and path.is_relative_to(root):
        target = path.resolve()
    elif file_path.startswith(("/", "\\")):
        target = (root / file_path.lstrip("/\\")).resolve()
    else:
        target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Notebook must be inside the local workspace.")
    if target.suffix.lower() != ".ipynb":
        raise ValueError("Notebook must have the .ipynb extension.")
    return target


def _validate(notebook: dict) -> None:
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4:
        raise ValueError("Only nbformat 4 notebooks are supported.")
    if type(notebook.get("nbformat_minor")) is not int or notebook["nbformat_minor"] < 0:
        raise ValueError("Invalid nbformat_minor.")
    if not isinstance(notebook.get("metadata"), dict) or not isinstance(
        notebook.get("cells"), list
    ):
        raise ValueError("Notebook requires metadata and a cells array.")
    ids = set()
    for cell in notebook["cells"]:
        if not isinstance(cell, dict) or cell.get("cell_type") not in _CELL_TYPES:
            raise ValueError("Invalid notebook cell type.")
        if not isinstance(cell.get("metadata"), dict):
            raise ValueError("Each cell requires metadata.")
        source = cell.get("source")
        if not (
            isinstance(source, str)
            or isinstance(source, list)
            and all(isinstance(line, str) for line in source)
        ):
            raise ValueError("Cell source must be a string or array of strings.")
        cell_id = cell.get("id")
        if cell_id is not None:
            if not isinstance(cell_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cell_id):
                raise ValueError("Invalid cell ID.")
            if cell_id in ids:
                raise ValueError("Duplicate cell IDs are ambiguous.")
            ids.add(cell_id)
        elif notebook["nbformat_minor"] >= 5:
            raise ValueError("Notebook format 4.5+ requires cell IDs.")
        if cell["cell_type"] == "code":
            if not isinstance(cell.get("outputs"), list):
                raise ValueError("Code cells require an outputs array.")
            count = cell.get("execution_count")
            if count is not None and type(count) is not int:
                raise ValueError("Invalid execution_count.")


def _apply(notebook: dict, args: NotebookEditInput) -> tuple[int, str | None]:
    if args.edit_mode != "delete" and args.new_source is None:
        raise ValueError("new_source is required for replace and insert.")
    cells = notebook["cells"]
    index = args.cell_index
    if args.cell_id is not None:
        if index is not None:
            raise ValueError("Specify cell_id or cell_index, not both.")
        index = next((i for i, c in enumerate(cells) if c.get("id") == args.cell_id), None)
        if index is None:
            raise ValueError("cell_id was not found.")
        if args.edit_mode == "insert":
            index += 1
    if args.edit_mode == "insert":
        if args.cell_type is None:
            raise ValueError("cell_type is required for insert.")
        index = len(cells) if index is None else index
        if index > len(cells):
            raise ValueError("Insertion index is past the end of the notebook.")
        cell = {"cell_type": args.cell_type, "metadata": {}, "source": []}
        if notebook["nbformat_minor"] >= 5:
            cell["id"] = uuid.uuid4().hex
        cells.insert(index, cell)
    else:
        if index is None or index >= len(cells):
            raise ValueError("Select an existing cell using cell_id or cell_index.")
        cell = cells[index]
        if args.edit_mode == "delete":
            if args.new_source or args.cell_type is not None:
                raise ValueError("delete does not accept source or cell_type changes.")
            return index, cells.pop(index).get("id")
    previous_source = cell["source"]
    previous_source = (
        "".join(previous_source) if isinstance(previous_source, list) else previous_source
    )
    previous_type = cell["cell_type"]
    cell["cell_type"] = args.cell_type or previous_type
    cell["source"] = args.new_source.splitlines(keepends=True)
    if cell["cell_type"] == "code":
        if (
            args.edit_mode == "insert"
            or previous_type != "code"
            or previous_source != args.new_source
        ):
            cell["outputs"] = []
            cell["execution_count"] = None
        cell.pop("attachments", None)
    else:
        cell.pop("outputs", None)
        cell.pop("execution_count", None)
        if cell["cell_type"] == "raw":
            cell.pop("attachments", None)
    return index, cell.get("id")


@tool("NotebookEditTool", args_schema=NotebookEditInput)
def notebook_edit(
    file_path: str,
    new_source: str | None = None,
    edit_mode: Literal["replace", "insert", "delete"] = "replace",
    cell_index: int | None = None,
    cell_id: str | None = None,
    cell_type: Literal["code", "markdown", "raw"] | None = None,
) -> dict:
    """Edit one cell of an existing local Jupyter .ipynb notebook without execution.

    Read the notebook first with read_file. Replace/delete selects an existing
    cell by ID or zero-based index. Insert at an index, after a cell ID, or append
    when neither is given; cell_type is required for insertion. Metadata and
    untouched cells are preserved. Changed code cells lose stale outputs and
    execution counts. Supports nbformat 4, not remote sandbox notebooks.
    """
    from novacode_cli.config.config import settings

    args = NotebookEditInput(
        file_path=file_path,
        new_source=new_source,
        edit_mode=edit_mode,
        cell_index=cell_index,
        cell_id=cell_id,
        cell_type=cell_type,
    )
    return _edit_notebook(args, settings.get_workspace_root())


def _edit_notebook(args: NotebookEditInput, workspace_root: Path) -> dict:
    temporary = None
    try:
        with _EDIT_LOCK:
            path = resolve_notebook_path(args.file_path, workspace_root)
            before = path.read_bytes()
            notebook = json.loads(before.decode("utf-8-sig"))
            _validate(notebook)
            index, edited_id = _apply(notebook, args)
            _validate(notebook)
            content = json.dumps(notebook, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            if path.read_bytes() != before:
                raise ValueError("Notebook changed during editing; read it again before retrying.")
            os.replace(temporary, path)
            return {
                "file_path": str(path),
                "edit_mode": args.edit_mode,
                "cell_index": index,
                "cell_id": edited_id,
                "cell_count": len(notebook["cells"]),
            }
    except (OSError, ValueError, TypeError) as exc:
        return {"error": str(exc)}
    finally:
        if temporary is not None:
            try:
                if temporary.exists():
                    temporary.chmod(temporary.stat().st_mode | stat.S_IWUSR)
                    temporary.unlink()
            except OSError:
                pass


def build_notebook_tool(workspace_root: Path):
    """Bind to the agent's actual workspace, including worktrees/Cowork roots."""
    from langchain_core.tools import StructuredTool

    root = Path(workspace_root).resolve()
    return StructuredTool.from_function(
        name="NotebookEditTool",
        description=notebook_edit.description,
        args_schema=NotebookEditInput,
        func=lambda **kwargs: _edit_notebook(NotebookEditInput(**kwargs), root),
    )

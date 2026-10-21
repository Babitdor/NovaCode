"""Notebook edits preserve unrelated data and fail without partial writes."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from novacode_cli.tools import notebook_tools as N


@pytest.fixture
def notebook(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings

    monkeypatch.setattr(settings, "get_workspace_root", lambda: tmp_path)
    path = tmp_path / "analysis.ipynb"
    data = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"custom": {"keep": True}},
        "cells": [
            {
                "id": "first",
                "cell_type": "code",
                "source": ["print(1)\n"],
                "metadata": {"tags": ["keep"]},
                "execution_count": 2,
                "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}],
            },
            {
                "id": "second",
                "cell_type": "markdown",
                "source": "# Notes",
                "metadata": {},
                "attachments": {"a.png": {"image/png": "YWJj"}},
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def invoke(path, **args):
    return N.notebook_edit.invoke({"file_path": str(path), **args})


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_replace_preserves_other_cells_metadata_and_clears_stale_outputs(notebook):
    path, before = notebook
    result = invoke(path, cell_id="first", new_source="print('café')\n")
    after = read(path)
    assert result["cell_index"] == 0
    assert after["metadata"] == before["metadata"]
    assert after["cells"][1] == before["cells"][1]
    cell = after["cells"][0]
    assert cell["source"] == ["print('café')\n"]
    assert cell["metadata"] == before["cells"][0]["metadata"]
    assert cell["id"] == "first" and cell["outputs"] == [] and cell["execution_count"] is None


def test_unchanged_code_retains_outputs(notebook):
    path, before = notebook
    invoke(path, cell_index=0, new_source="print(1)\n")
    assert read(path) == before


@pytest.mark.parametrize("cell_type", ["code", "markdown", "raw"])
@pytest.mark.parametrize(
    "selector,index", [({}, 2), ({"cell_index": 0}, 0), ({"cell_id": "first"}, 1)]
)
def test_insert_types_and_positions(notebook, cell_type, selector, index):
    path, before = notebook
    result = invoke(path, edit_mode="insert", cell_type=cell_type, new_source="new\n", **selector)
    after = read(path)
    cell = after["cells"].pop(index)
    assert result["cell_index"] == index and result["cell_count"] == 3
    assert after == before
    assert cell["cell_type"] == cell_type and cell["id"] not in {"first", "second"}
    if cell_type == "code":
        assert cell["outputs"] == [] and cell["execution_count"] is None


def test_delete_selected_cell(notebook):
    path, before = notebook
    assert invoke(path, edit_mode="delete", cell_id="first")["cell_count"] == 1
    assert read(path)["cells"] == [before["cells"][1]]


@pytest.mark.parametrize("target", ["code", "raw"])
def test_cell_conversion_removes_incompatible_fields(notebook, target):
    path, _ = notebook
    invoke(path, cell_index=1, cell_type=target, new_source="new")
    cell = read(path)["cells"][1]
    assert "attachments" not in cell
    assert cell["cell_type"] == target and cell["id"] == "second"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"cell_index": 10},
        {"cell_id": "missing"},
        {"cell_index": 0, "cell_id": "first"},
        {"edit_mode": "insert", "cell_index": 0},
        {"edit_mode": "insert", "cell_type": "code", "cell_index": 4},
        {"edit_mode": "delete", "cell_index": 0, "new_source": "wrong"},
    ],
)
def test_invalid_edits_leave_original_bytes(notebook, args):
    path, _ = notebook
    before = path.read_bytes()
    assert "error" in invoke(path, **args)
    assert path.read_bytes() == before


@pytest.mark.parametrize("index", ["0", True, -1])
def test_schema_rejects_invalid_indices(notebook, index):
    path, _ = notebook
    with pytest.raises(ValidationError):
        invoke(path, cell_index=index)


def test_malformed_and_legacy_notebooks_are_not_overwritten(notebook):
    path, _ = notebook
    for content in ["{broken", "[]", '{"nbformat": 3}', '{"nbformat": 4, "cells": null}']:
        path.write_text(content, encoding="utf-8")
        assert "error" in invoke(path, cell_index=0)
        assert path.read_text(encoding="utf-8") == content


def test_workspace_paths_and_traversal(notebook):
    path, _ = notebook
    assert "error" not in invoke("/analysis.ipynb", cell_index=0, new_source="virtual")
    assert "error" not in invoke("analysis.ipynb", cell_index=0, new_source="relative")
    assert "error" in invoke("../outside.ipynb", cell_index=0)
    assert "error" in invoke(path.with_suffix(".json"), cell_index=0)


def test_failed_atomic_replace_keeps_original_and_removes_temporary_file(notebook, monkeypatch):
    path, _ = notebook
    before = path.read_bytes()

    def fail(*_):
        raise OSError("locked")

    monkeypatch.setattr(N.os, "replace", fail)
    assert "locked" in invoke(path, cell_index=0, new_source="changed")["error"]
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


def test_concurrent_inserts_do_not_lose_cells(notebook):
    path, _ = notebook

    def insert(index):
        return invoke(path, edit_mode="insert", cell_type="raw", new_source=str(index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(insert, range(8)))
    assert all("error" not in result for result in results)
    assert len(read(path)["cells"]) == 10


def test_approval_and_plan_mode_integration():
    from novacode_cli.agents.tool_search import CORE_TOOLS
    from novacode_cli.config.plan_mode import BLOCKED_TOOLS
    from novacode_cli.hitl.interrupts import INTERRUPT_SPECS
    from novacode_cli.security.policy import ApprovalPolicy

    policy = ApprovalPolicy(
        tool_tiers={},
        shell_allow=[],
        shell_deny=[],
        path_allow=[],
        path_deny=["**/secret.ipynb"],
        domain_allow=[],
        domain_deny=[],
    )
    assert policy.evaluate("NotebookEditTool", {"file_path": "analysis.ipynb"}).tier == "ask"
    assert policy.evaluate("NotebookEditTool", {"file_path": "/secret.ipynb"}).tier == "deny"
    assert "NotebookEditTool" in BLOCKED_TOOLS and "NotebookEditTool" in INTERRUPT_SPECS
    assert "NotebookEditTool" not in CORE_TOOLS


async def test_async_notebook_tool(notebook):
    path, _ = notebook
    result = await N.notebook_edit.ainvoke(
        {"file_path": str(path), "cell_id": "first", "new_source": ""}
    )
    assert "error" not in result
    assert read(path)["cells"][0]["source"] == []


def test_missing_source_cannot_silently_erase_a_cell(notebook):
    path, _ = notebook
    before = path.read_bytes()
    assert "new_source is required" in invoke(path, cell_index=0)["error"]
    assert path.read_bytes() == before


def test_older_v4_notebooks_do_not_gain_required_cell_ids(notebook):
    path, data = notebook
    data["nbformat_minor"] = 4
    for cell in data["cells"]:
        cell.pop("id")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "error" not in invoke(path, edit_mode="insert", cell_type="raw", new_source="old format")
    assert all("id" not in cell for cell in read(path)["cells"])


def test_bound_tool_uses_its_own_workspace(notebook):
    path, data = notebook
    other = path.parent / "worktree"
    other.mkdir()
    target = other / path.name
    target.write_text(json.dumps(data), encoding="utf-8")
    bound = N.build_notebook_tool(other)
    result = bound.invoke(
        {"file_path": "/analysis.ipynb", "cell_index": 0, "new_source": "worktree"}
    )
    assert "error" not in result
    assert read(path) == data
    assert read(target)["cells"][0]["source"] == ["worktree"]

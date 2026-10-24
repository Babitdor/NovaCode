"""Edits are followed by the project's own configured checkers."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from novacode_cli.agents import edit_diagnostics
from novacode_cli.agents.edit_diagnostics import (
    MAX_OUTPUT_CHARS,
    EditDiagnosticsMiddleware,
    edited_lines,
    prioritized,
)
from novacode_cli.config.nova_config import NovaConfig

RUFF = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which("ruff")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import novacode_cli.config.config as config_module

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")


class _Request:
    def __init__(self, name: str, file_path: str) -> None:
        self.tool_call = {"id": "1", "name": name, "args": {"file_path": file_path}}


def _project(tmp_path: Path, *, ruff: bool) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if ruff:
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nselect = ["F"]\n', encoding="utf-8")
    (tmp_path / "pkg").mkdir(parents=True)
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    return tmp_path


async def _edit(tmp_path: Path, result: ToolMessage, name: str = "edit_file") -> ToolMessage:
    middleware = EditDiagnosticsMiddleware(tmp_path)
    return await middleware.awrap_tool_call(
        _Request(name, "/pkg/mod.py"), AsyncMock(return_value=result)
    )


def _ok() -> ToolMessage:
    return ToolMessage(content="Successfully replaced 1 instance", tool_call_id="1")


@pytest.mark.skipif(RUFF is None, reason="ruff is not installed")
@pytest.mark.timeout(30)
async def test_configured_ruff_errors_follow_the_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _project(tmp_path, ruff=True)
    monkeypatch.setattr(
        edit_diagnostics, "_executable", lambda _root, name: RUFF if name == "ruff" else None
    )
    result = await _edit(root, _ok())
    assert result.content.startswith("Successfully replaced 1 instance")
    assert "ruff:" in result.content
    assert "F401" in result.content


@pytest.mark.timeout(30)
async def test_unconfigured_project_disabled_setting_and_failed_edits_add_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _project(tmp_path, ruff=False)
    monkeypatch.setattr(edit_diagnostics, "_executable", lambda _root, _name: RUFF or "ruff")
    assert (await _edit(root, _ok())).content == _ok().content

    (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    failed = ToolMessage(content="Error: String not found", tool_call_id="1", status="error")
    assert await _edit(root, failed) is failed
    assert (await _edit(root, _ok(), name="read_file")).content == _ok().content

    NovaConfig().set("edit_diagnostics_enabled", value=False)
    assert (await _edit(root, _ok())).content == _ok().content


def test_paths_outside_the_workspace_are_ignored(tmp_path: Path):
    root = _project(tmp_path / "root", ruff=True)
    outside = tmp_path / "other.py"
    outside.write_text("import os\n", encoding="utf-8")
    assert edit_diagnostics._resolve(str(outside), root.resolve()) is None
    assert edit_diagnostics._resolve("/../other.py", root.resolve()) is None
    assert (
        edit_diagnostics._resolve("/pkg/mod.py", root.resolve())
        == (root / "pkg" / "mod.py").resolve()
    )


def test_new_error_deep_in_a_noisy_file_is_shown_first(tmp_path: Path):
    path = tmp_path / "big.py"
    lines = [f"x{i} = 1" for i in range(3000)]
    lines[2500] = "y = undefined_name"
    path.write_text("\n".join(lines), encoding="utf-8")
    ranges = edited_lines({"new_string": "y = undefined_name"}, path)
    assert ranges == [(2501, 2501)]
    output = "\n".join(
        [f"big.py:{n}:1: ANN001 Missing type annotation" for n in range(1, 400)]
        + ["big.py:2501:5: F821 Undefined name `undefined_name`", "Found 400 errors."]
    )
    text = prioritized(output, ranges)
    assert text.startswith("Near your edit:\nbig.py:2501:5: F821")
    assert "Elsewhere in the file:\nbig.py:1:1: ANN001" in text
    assert len(text) <= MAX_OUTPUT_CHARS + 60
    assert text.endswith("more problems in this file not shown)")
    assert "Found 400 errors." not in text


def test_pyright_and_tsc_details_stay_with_their_problem(tmp_path: Path):
    output = (
        "/p/a.py\n"
        "  /p/a.py:10:1 - error: Import missing\n"
        "    detail\n"
        "src/a.ts(30,3): error TS2322: Type mismatch.\n"
        "  more detail\n"
        "1 error, 0 warnings"
    )
    text = prioritized(output, [(30, 30)])
    assert text.splitlines()[:3] == [
        "Near your edit:",
        "src/a.ts(30,3): error TS2322: Type mismatch.",
        "  more detail",
    ]
    assert "  /p/a.py:10:1 - error: Import missing\n    detail" in text
    assert text.endswith("/p/a.py\n1 error, 0 warnings")
    assert edited_lines({"content": "x"}, tmp_path / "missing.py") == []


def test_tsc_output_is_filtered_to_the_edited_file(tmp_path: Path):
    path = tmp_path / "src" / "a.ts"
    output = (
        "src/a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        "  detail for a\n"
        "src/b.ts(2,1): error TS2304: Cannot find name 'x'.\n"
        "  detail for b"
    )
    assert edit_diagnostics._only_file(output, path, tmp_path) == (
        "src/a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        "  detail for a"
    )

"""Reproduce offline MCP Code Mode checks and assertion-killed negative controls."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_mcp_code_mode.py"
SOURCES = ["novacode_cli/mcp/code_mode.py", "novacode_cli/mcp/code_runtime.py"]


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True, timeout=180)


def main() -> None:
    for source in SOURCES:
        print(source, hashlib.sha256((ROOT / source).read_bytes()).hexdigest(), flush=True)
    with tempfile.TemporaryDirectory(prefix="nova-mcp-code-check-") as directory:
        data = "--data-file=" + str(Path(directory) / "coverage")
        run(
            "-m",
            "coverage",
            "run",
            data,
            "--branch",
            "--source=novacode_cli.mcp.code_mode,novacode_cli.mcp.code_runtime",
            "-m",
            "pytest",
            TEST,
            "-q",
            "--tb=short",
        )
        run("-m", "coverage", "report", data, "--fail-under=85", "-m")
    run("-m", "ruff", "check", *SOURCES)
    run("-m", "ruff", "check", "--select", "F,I", TEST, __file__)
    run("-m", "ruff", "format", "--check", *SOURCES, TEST, __file__)
    run("-m", "mypy", *SOURCES, "--follow-imports=silent")
    controls = [
        (
            "argument validation removed",
            "m._validator=lambda *a,**kw:type('NoValidation',(),{'validate':lambda *a:None})()",
            "schema_and_inventory_validation",
        ),
        (
            "call budget removed",
            "original=m.MCPCodeModeMiddleware.__init__; "
            "m.MCPCodeModeMiddleware.__init__=lambda self,*a,**kw:original(self,*a,**{**kw,'max_calls':999})",
            "budget_errors",
        ),
        (
            "approval evaluator bypassed",
            "exec('async def bypass(*args): pass'); m.MCPCodeModeMiddleware._authorize=bypass",
            "ask_requires_normal_approval_path",
        ),
    ]
    for name, mutation, selector in controls:
        code = (
            "import pytest; import novacode_cli.mcp.code_mode as m; "
            + mutation
            + "; raise SystemExit(pytest.main(["
            + repr(TEST)
            + ",'-q','--tb=no','-k',"
            + repr(selector)
            + "]))"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=90)
        if result.returncode != 1:
            raise RuntimeError(f"Negative control {name!r} did not fail a test assertion")
        print("Negative control killed:", name, flush=True)


if __name__ == "__main__":
    main()

"""Reproduce native/MCP Code Mode checks and assertion-killed negative controls."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_mcp_code_mode.py"
NATIVE_TEST = "tests/test_native_code_mode.py"
TESTS = [TEST, NATIVE_TEST, "tests/test_code_mode_toggle.py"]
REGRESSION_TESTS = [
    "tests/test_deepagents_compat.py",
    "tests/test_generic_mcp_tool.py",
    "tests/test_attribution.py",
    "tests/test_strict_tools.py",
    "tests/test_tool_search.py",
    "tests/test_tool_search_variants.py",
    "tests/test_security_middleware.py",
    "tests/test_approval_policy.py",
    "tests/test_team_workspace.py",
    "tests/test_cowork_policy.py",
]
SOURCES = [
    "novacode_cli/agents/code_mode.py",
    "novacode_cli/agents/code_runtime.py",
    "novacode_cli/mcp/code_mode.py",
    "novacode_cli/mcp/code_runtime.py",
]


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
            "--source=novacode_cli.agents.code_mode,novacode_cli.agents.code_runtime",
            "-m",
            "pytest",
            *TESTS,
            "-q",
            "--tb=short",
            "--random-order",
            "--random-order-seed=24102026",
        )
        run("-m", "coverage", "report", data, "--fail-under=85", "-m")
    run("-m", "ruff", "check", *SOURCES)
    run("-m", "ruff", "check", NATIVE_TEST, "tests/test_code_mode_toggle.py")
    run("-m", "ruff", "check", "--select", "F,I", TEST, __file__)
    run("-m", "ruff", "format", "--check", *SOURCES, *TESTS, __file__)
    run("-m", "mypy", *SOURCES, "--follow-imports=silent")
    run("-m", "pytest", *REGRESSION_TESTS, "-q", "--tb=short")
    controls = [
        (
            "argument validation removed",
            "m._validator=lambda *a,**kw:type('NoValidation',(),{'validate':lambda *a:None})()",
            NATIVE_TEST + "::test_bad_arguments_rejected_before_execution",
        ),
        (
            "call budget removed",
            "original=m.CodeModeMiddleware.__init__; "
            "m.CodeModeMiddleware.__init__=lambda self,*a,**kw:original(self,*a,**{**kw,'max_calls':999})",
            NATIVE_TEST + "::test_native_call_budget_and_failure_accounting",
        ),
        (
            "approval evaluator bypassed",
            "exec('async def bypass(*args): pass'); m.CodeModeMiddleware._authorize=bypass",
            NATIVE_TEST + "::test_native_approval_and_denial_happen_before_execution[ask-False]",
        ),
        (
            "control-tool exclusion removed",
            "m.DIRECT_ONLY_TOOLS=frozenset()",
            NATIVE_TEST + "::test_control_tools_are_not_exposed_or_called[eval]",
        ),
    ]
    for name, mutation, selector in controls:
        code = (
            "import pytest; import novacode_cli.agents.code_mode as m; "
            "print('Mutating loaded module:', m.__file__, flush=True); "
            + mutation
            + "; raise SystemExit(pytest.main(["
            + repr(selector)
            + ",'-q','--tb=short']))"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=90)
        if result.returncode != 1:
            raise RuntimeError(f"Negative control {name!r} did not fail a test assertion")
        print("Negative control killed:", name, flush=True)


if __name__ == "__main__":
    main()

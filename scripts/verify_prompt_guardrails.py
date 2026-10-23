"""Offline guardrail checks, coverage enforcement and three negative controls.

Run: uv run python scripts/verify_prompt_guardrails.py
No real credentials, provider calls, source mutations or configuration writes.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_prompt_injection_guardrails.py"


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True, timeout=180)


def main() -> None:
    source = ROOT / "novacode_cli/security/prompt_injection.py"
    print("Guardrail source SHA256:", hashlib.sha256(source.read_bytes()).hexdigest(), flush=True)
    with tempfile.TemporaryDirectory(prefix="nova-guardrail-coverage-") as directory:
        data = "--data-file=" + str(Path(directory) / "coverage")
        run(
            "-m",
            "coverage",
            "run",
            data,
            "--source=novacode_cli.security.prompt_injection",
            "-m",
            "pytest",
            TEST,
            "-q",
            "--tb=short",
        )
        run("-m", "coverage", "report", data, "--fail-under=95", "-m")
    run("-m", "ruff", "check", "--select", "F,I", str(source), TEST, __file__)
    run("-m", "mypy", str(source), "--follow-imports=silent")
    run(
        "-m",
        "pytest",
        "tests/test_security_middleware.py",
        "tests/test_delegated_auto_approve.py",
        "tests/test_approval_policy.py",
        "tests/test_subagent_resilience.py",
        "tests/test_dream_agent_backend.py",
        "tests/test_plan_agent_checkpoint.py",
        "-q",
        "--tb=short",
    )
    mutations = [
        (
            "request boundary removed",
            "m.PromptInjectionMiddleware.prepare=lambda self,r:r",
            "both_model_wrappers",
        ),
        (
            "credential denial removed",
            "m._contains_secret=lambda *args:False",
            "secret_tool_arguments",
        ),
        (
            "credential redaction removed",
            "m._redact=lambda text,secrets:text",
            "runtime_credentials",
        ),
    ]
    for name, mutation, selector in mutations:
        code = (
            "import pytest; import novacode_cli.security.prompt_injection as m; "
            + mutation
            + "; raise SystemExit(pytest.main(["
            + repr(TEST)
            + ", '-q', '--tb=no', '-k', "
            + repr(selector)
            + "]))"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=60)
        if result.returncode != 1:
            raise RuntimeError(f"Negative control {name!r} was not killed by an assertion failure")
        print("Negative control killed:", name, flush=True)


if __name__ == "__main__":
    main()

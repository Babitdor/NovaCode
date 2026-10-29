"""Reproduce the runtime boundary checks, baseline lint and mutation controls.

Run: python -m scripts.verify_runtime_fixes --static --mutations --coverage --full
All checks are offline. Mutants exist only in isolated child interpreters.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_runtime_boundary_fixes.py",
    "tests/test_python_kernel_tool.py",
    "tests/test_approval_policy.py",
    "tests/test_plan_middleware.py",
    "tests/test_plan_mcp_guard.py",
    "tests/test_plan_agent_checkpoint.py",
    "tests/test_deepagents_compat.py",
    "tests/test_tui_cancel.py",
    "tests/test_headless_production.py",
    "tests/test_session_crash_recovery.py",
    "tests/test_workdir_backend.py",
    "tests/test_checkpoint_coordinator.py",
    "tests/test_cowork_broker_middleware.py",
    "tests/test_local_backend_subagent_execute.py",
    "tests/test_events.py",
    "tests/test_session_allow.py",
    "tests/test_shell_jobs.py",
    "tests/test_remember_approvals.py",
    "tests/test_native_code_mode.py",
    "tests/test_shutdown.py",
]
SOURCES = [
    "novacode_cli/agents/plan_agent/plan_agent.py",
    "novacode_cli/config/plan_mode.py",
    "novacode_cli/core/agent_loop.py",
    "novacode_cli/events.py",
    "novacode_cli/main.py",
    "novacode_cli/security/policy.py",
    "novacode_cli/security/remember.py",
    "novacode_cli/security/session_allow.py",
    "novacode_cli/session/checkpoint_coordinator.py",
    "novacode_cli/shell/middleware.py",
    "novacode_cli/tools/python_kernel_tool.py",
    "novacode_cli/tui/app.py",
    "novacode_cli/ui/hitl_approval.py",
]
MUTANTS = [
    (
        "git-prefix",
        "from novacode_cli.security import policy; "
        "policy._DEFAULT_SHELL_ALLOW[0] = r'^\\s*git\\s+(status|diff|log|show|remote\\s+-v)'",
        "tests/test_runtime_boundary_fixes.py::test_git_noninspection_requires_approval",
    ),
    (
        "plan-execution",
        "from novacode_cli.config import plan_mode; "
        "plan_mode.BLOCKED_TOOLS = plan_mode.BLOCKED_TOOLS - {'python_kernel', 'daemon'}",
        "tests/test_runtime_boundary_fixes.py::test_plan_rejects_native_mutations",
    ),
    (
        "execute-bypass",
        "from novacode_cli.shell.middleware import ShellMiddleware\n"
        "async def unsafe(self, request, handler):\n"
        "    return await self._adispatch_local(request.tool_call['args']['command'])\n"
        "ShellMiddleware.awrap_tool_call = unsafe",
        "tests/test_runtime_boundary_fixes.py::test_local_execute_traverses_guards[True-workspace]",
    ),
    (
        "event-leak",
        "from novacode_cli.events import _SessionEventLog; "
        "_SessionEventLog._log = lambda self: self._logs.setdefault(None, [])",
        "tests/test_runtime_boundary_fixes.py::test_events_are_isolated_and_inherited_by_worker_threads",
    ),
    (
        "kernel-race",
        "from novacode_cli.tools.python_kernel_tool import _KernelProcess; "
        "_KernelProcess.request = _KernelProcess._request",
        "tests/test_python_kernel_tool.py::test_parallel_requests_keep_their_responses",
    ),
]


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Execute a fixed check without a shell; failures are never hidden."""
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
    return subprocess.run(args, cwd=ROOT, check=False, **kwargs)  # noqa: S603 -- fixed checks, no shell


def baseline_check() -> int:
    """Reproduce the unrelated full-suite failure from an untouched HEAD archive."""
    archive = run(["git", "archive", "HEAD"], capture_output=True)
    if archive.returncode:
        raise RuntimeError(archive.stderr.decode("utf-8", errors="replace"))
    with tempfile.TemporaryDirectory(prefix="nova-runtime-baseline-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as snapshot:
            snapshot.extractall(directory, filter="data")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                "tests/test_async_general_purpose.py::test_async_general_purpose_graph_keeps_workspace_and_approval_controls",
            ],
            cwd=directory,
            check=False,
        ).returncode


def static_checks() -> None:
    """Check clean new modules and compare existing lint counts against HEAD."""
    ruff = str(Path(sys.executable).with_name("ruff.exe" if sys.platform == "win32" else "ruff"))
    additions = []
    for name in SOURCES:
        original = run(["git", "show", f"HEAD:{name}"], capture_output=True, text=True)
        if original.returncode:
            raise RuntimeError(original.stderr)
        counts = []
        for content in (original.stdout, (ROOT / name).read_text(encoding="utf-8")):
            result = run(
                [ruff, "check", "--output-format=json", "--stdin-filename", name, "-"],
                input=content,
                capture_output=True,
                text=True,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError(result.stderr)
            counts.append(
                Counter((item["code"], item["message"]) for item in json.loads(result.stdout))
            )
        additions.extend((name, issue, count) for issue, count in (counts[1] - counts[0]).items())
    if additions:
        message = f"Increased lint diagnostics: {additions}"
        raise RuntimeError(message)
    for command in (
        [ruff, "check", "tests/test_runtime_boundary_fixes.py", "scripts/verify_runtime_fixes.py"],
        [
            ruff,
            "format",
            "--check",
            "novacode_cli/events.py",
            "novacode_cli/tools/python_kernel_tool.py",
            "novacode_cli/security/session_allow.py",
            "novacode_cli/security/remember.py",
            "tests/test_runtime_boundary_fixes.py",
            "tests/test_python_kernel_tool.py",
        ],
        [
            sys.executable,
            "-m",
            "mypy",
            "novacode_cli/events.py",
            "novacode_cli/security/session_allow.py",
            "novacode_cli/tools/python_kernel_tool.py",
            "--follow-imports=silent",
        ],
    ):
        if run(command).returncode:
            message = f"Static check failed: {command}"
            raise RuntimeError(message)
    print("Static checks passed; existing-file lint counts did not increase.", flush=True)  # noqa: T201


def main() -> int:
    """Fail if any requested layer fails or a mutant survives."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    if args.baseline:
        return baseline_check()
    if args.static:
        static_checks()
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *TESTS]
    if args.coverage:
        command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--data-file=.tmp/runtime-fixes.coverage",
            "--source=novacode_cli",
            *command[1:],
        ]
    if run(command).returncode:
        return 1
    if (
        args.coverage
        and run(
            [
                sys.executable,
                "-m",
                "coverage",
                "report",
                "--data-file=.tmp/runtime-fixes.coverage",
                "--include=novacode_cli/events.py,novacode_cli/security/session_allow.py,novacode_cli/security/remember.py,novacode_cli/tools/python_kernel_tool.py",
                "--fail-under=85",
                "--show-missing",
            ]
        ).returncode
    ):
        return 1
    if args.mutations:
        for name, mutation, selector in MUTANTS:
            code = (
                mutation
                + "\nimport pytest\nraise SystemExit(pytest.main("
                + repr(["-q", "--tb=short", "-p", "no:cacheprovider", selector])
                + "))"
            )
            result = run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
            expected_failure = "FAILED " + selector.partition("[")[0]
            if result.returncode != 1 or expected_failure not in result.stdout:
                print(result.stdout, result.stderr)  # noqa: T201 -- failure evidence
                message = f"Mutant {name}: expected test failure (1), got {result.returncode}"
                raise RuntimeError(message)
            print(f"KILLED {name}", flush=True)  # noqa: T201 -- CLI check result
    if args.full:
        return run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
        ).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

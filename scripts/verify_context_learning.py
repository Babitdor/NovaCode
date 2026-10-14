"""Reproduce local context/learning evidence; never calls a live provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    "tests/test_checkpoint_coordinator.py",
    "tests/test_provider_accounting.py",
    "tests/test_learning_activation.py",
    "tests/test_learning_command_lifecycle.py",
    "tests/test_context_learning_verification_gate.py",
    "tests/test_headless_production.py",
    "tests/test_recovery_archives.py",
    "tests/test_context_learning_properties.py",
    "tests/test_google_workspace_offline.py",
    "tests/test_google_oauth_auth.py",
    "tests/test_mcp_presets.py",
    "tests/test_phase45.py",
    "tests/test_subagent_resilience.py",
    "tests/test_context_learning_replay.py",
    "tests/test_context_learning_audit.py",
    "tests/test_request_snapshot.py",
    "tests/test_learning_ledger.py",
    "tests/test_learning_review_safety.py",
    "tests/test_context_compaction.py",
    "tests/test_compaction.py",
    "tests/test_hermes",
    "tests/test_review_task_end.py",
    "tests/test_context_snapshot.py",
    "tests/test_memory_retrieval.py",
    "tests/test_memory_prompt_cache_order.py",
    "tests/test_learning_recall.py",
    "tests/test_project_memory.py",
    "tests/test_builtin_lessons.py",
    "tests/test_good_habits_injection.py",
    "tests/test_async_offload_fixes.py",
    "tests/test_async_model_context.py",
    "tests/test_async_mcp_tools.py",
]
NEW_MODULES = [
    "novacode_cli/session/runtime_identity.py",
    "novacode_cli/session/checkpoint_coordinator.py",
    "novacode_cli/hermes/activation.py",
    "novacode_cli/context/archives.py",
    "novacode_cli/context/provider_accounting.py",
    "novacode_cli/memory/parsed_cache.py",
    "novacode_cli/context/snapshot.py",
    "novacode_cli/hermes/ledger.py",
    "novacode_cli/commands/learning_handler.py",
]
LINT_MODULES = [*NEW_MODULES, "scripts/verify_context_learning.py"]
REQUIRED_GATES = (
    "tests",
    "lint_new_modules",
    "changed_line_coverage",
    "types_new_modules",
    "mutations",
    "shuffled_affected",
    "full_suite_runs",
)
EXPECTED_FULL_RUNS = 2
EXPECTED_MUTATIONS = 8


def execute(command: list[str]) -> dict:
    """Run one known local verification command and retain its output."""
    started = time.perf_counter()
    process = subprocess.run(  # noqa: S603 - commands are assembled by this local verifier
        command, cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", check=False
    )
    print(process.stdout)  # noqa: T201 - CLI progress
    if process.stderr:
        print(process.stderr, file=sys.stderr)  # noqa: T201 - CLI diagnostics
    return {
        "command": command,
        "exit_code": process.returncode,
        "duration_seconds": time.perf_counter() - started,
        "output": process.stdout,
    }


def source_hash() -> str:
    """Fingerprint Git-visible source and runtime assets, tests, and contract."""
    digest = hashlib.sha256()
    listed = subprocess.run(
        [  # noqa: S607 - Git defines the candidate source set
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "novacode_cli",
            "tests",
            "scripts",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    for name in sorted(os.fsdecode(item) for item in listed.split(b"\0") if item):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(path.read_bytes())
    for filename in (
        "pyproject.toml",
        "uv.lock",
        "CONTEXT_LEARNING_SPEC.md",
        "CONTEXT_LEARNING_CONTRACT.md",
    ):
        path = ROOT / filename
        digest.update(filename.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def changed_line_coverage(report: Path, threshold: float = 90.0) -> dict:
    """Fail closed on changed executable lines, including untracked modules."""
    patch = subprocess.run(
        ["git", "diff", "HEAD", "--unified=0", "--no-color", "--", "novacode_cli"],  # noqa: S607 - Git is the repository source of truth
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    changed = {}
    filename = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            filename = line[6:]
        elif filename and line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match is None:
                message = "Unreadable diff hunk"
                raise RuntimeError(message)
            start, count = int(match.group(1)), int(match.group(2) or 1)
            changed.setdefault(filename, set()).update(range(start, start + count))
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "novacode_cli"],  # noqa: S607 - Git is the repository source of truth
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for name in untracked.splitlines():
        if name.endswith(".py"):
            changed[name] = set(
                range(1, len((ROOT / name).read_text(encoding="utf-8").splitlines()) + 1)
            )
    coverage = json.loads(report.read_text(encoding="utf-8"))["files"]
    missing = {}
    total = covered = 0
    for name, lines in changed.items():
        if not name.endswith(".py"):
            continue
        entry = coverage.get(name) or coverage.get(name.replace("/", "\\"))
        if entry is None:
            from coverage import Coverage

            analysis = Coverage().analysis2(str(ROOT / name))
            entry = {"executed_lines": [], "missing_lines": analysis[1]}
        executed, unexecuted = set(entry["executed_lines"]), set(entry["missing_lines"])
        relevant = lines & (executed | unexecuted)
        total += len(relevant)
        covered += len(relevant & executed)
        if relevant & unexecuted:
            missing[name] = sorted(relevant & unexecuted)
    if total == 0:
        message = "Changed-line coverage had no executable lines"
        raise RuntimeError(message)
    percentage = covered * 100 / total
    return {
        "covered": covered,
        "total": total,
        "percentage": percentage,
        "threshold": threshold,
        "passed": percentage >= threshold,
        "missing": missing,
    }


def mutations() -> list[dict]:
    """Apply targeted faults and require the matching regression to fail."""
    cases = [
        (
            "novacode_cli/agents/context_compaction.py",
            "return said\n",
            "return said[:1]\n",
            "tests/test_context_learning_audit.py::test_compaction_preserves_middle_constraints_and_all_steering",
        ),
        (
            "novacode_cli/hermes/tracker.py",
            "self.owner = owner or uuid.uuid4().hex",
            "self.owner = 'shared'",
            "tests/test_context_learning_audit.py::test_independent_counters_do_not_share_state",
        ),
        (
            "novacode_cli/hermes/ledger.py",
            "if prior.revision != int(revision):",
            "if False:",
            "tests/test_learning_ledger.py::test_revision_approval_reject_rollback_and_project_isolation",
        ),
        (
            "novacode_cli/memory/agent_memory.py",
            "return file_hash(path)",
            "return 'constant'",
            "tests/test_context_learning_audit.py::test_memory_detects_restored_timestamp_and_deletion",
        ),
        (
            "novacode_cli/hermes/activation.py",
            'for field in ("name", "description")',
            'for field in ("name",)',
            "tests/test_learning_activation.py::test_malformed_generated_skill_cannot_be_marked_applied",
        ),
        (
            "scripts/verify_context_learning.py",
            'for name in sorted(os.fsdecode(item) for item in listed.split(b"\\0") if item):',
            "for name in ():",
            "tests/test_context_learning_verification_gate.py::test_source_hash_includes_runtime_templates_and_packaged_assets",
        ),
        (
            "scripts/verify_context_learning.py",
            '["git", "diff", "HEAD", "--unified=0", "--no-color"',
            '["git", "diff", "--unified=0", "--no-color"',
            "tests/test_context_learning_verification_gate.py::test_coverage_gate_counts_staged_and_unstaged_lines",
        ),
        (
            "scripts/verify_context_learning.py",
            '        and all(run["exit_code"] == 0 for run in full_runs)\n',
            "        and True\n",
            "tests/test_context_learning_verification_gate.py::test_default_verdict_rejects_partial_or_failed_acceptance_evidence",
        ),
    ]
    results = []
    for filename, original, mutant, target in cases:
        path = ROOT / filename
        before = path.read_bytes()
        source = before.decode("utf-8").replace("\r\n", "\n")
        if original not in source:
            message = f"Mutation target not found: {filename}"
            raise RuntimeError(message)
        command = [sys.executable, "-m", "pytest", target, "-q", "--timeout=120"]
        if execute(command)["exit_code"] != 0:
            message = f"Mutation baseline failed: {target}"
            raise RuntimeError(message)
        try:
            path.write_text(source.replace(original, mutant, 1), encoding="utf-8")
            result = execute(command)
            result["mutation"] = filename
            result["killed"] = result["exit_code"] == 1 and bool(
                re.search(r"\b[1-9]\d* failed\b", result["output"])
            )
            results.append(result)
        finally:
            path.write_bytes(before)
        if path.read_bytes() != before:
            message = f"Mutation restore failed: {filename}"
            raise RuntimeError(message)
    return results


def measure() -> dict:
    """Capture a content-free observational request-accounting sample."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    from novacode_cli.context.snapshot import ContextSnapshot

    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        tools=[],
        state={},
        messages=[
            HumanMessage("Keep all constraints"),
            *[AIMessage("sample " * 100) for _ in range(100)],
        ],
    )
    started = time.perf_counter()
    for _ in range(50):
        snapshot = ContextSnapshot.capture(request)
    return {
        "snapshot_mean_ms": (time.perf_counter() - started) * 1000 / 50,
        "estimated_input_tokens": snapshot.estimated_input_tokens,
        "protected_tokens": snapshot.protected_tokens,
        "messages": len(snapshot.messages),
        "live_provider_calls": 0,
        "coding_quality_improvement": "not measured",
    }


def acceptance_verdict(results: dict, *, complete_mode: bool) -> bool:
    """Fail closed unless every required gate has current successful evidence."""
    missing = [name for name in REQUIRED_GATES if name not in results]
    full_runs = results.get("full_suite_runs", [])
    if len(full_runs) != EXPECTED_FULL_RUNS:
        missing.append("two_clean_full_suite_runs")
    results["missing_acceptance_gates"] = missing
    return bool(
        complete_mode
        and not missing
        and results.get("partial_checks_passed")
        and results.get("source_unchanged")
        and results["tests"]["exit_code"] == 0
        and results["lint_new_modules"]["exit_code"] == 0
        and results["changed_line_coverage"].get("passed")
        and results["types_new_modules"]["exit_code"] == 0
        and len(results["mutations"]) == EXPECTED_MUTATIONS
        and all(case.get("killed") for case in results["mutations"])
        and results["shuffled_affected"]["exit_code"] == 0
        and all(run["exit_code"] == 0 for run in full_runs)
    )


def main() -> int:  # noqa: PLR0912, PLR0915 - linear fail-closed acceptance orchestration
    """Run partial diagnostics or the complete offline acceptance gauntlet."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--affected-only", action="store_true")
    parser.add_argument("--output", default="context-learning-results.json")
    args = parser.parse_args()
    acceptance = not (args.affected_only or args.full or args.mutations or args.coverage)
    results = {
        "source_tree_sha256": source_hash(),
        "python": sys.version,
        "live_evaluations": False,
        "scope": "complete-acceptance" if acceptance else "partial-diagnostic",
    }
    if args.mutations:
        results["mutations"] = mutations()
        results["restored_source_tree_sha256"] = source_hash()
        partial_passed = all(case["killed"] for case in results["mutations"])
    else:
        target = ["tests"] if args.full else TARGETS
        runner = (
            [sys.executable, "-m", "coverage", "run", "--source=novacode_cli", "-m", "pytest"]
            if args.coverage
            else [sys.executable, "-m", "pytest"]
        )
        results["tests"] = execute(
            [
                *runner,
                *target,
                "-q",
                "--tb=short",
                "--timeout=120",
                "--junitxml=context-learning-tests.xml",
            ]
        )
        ruff = ROOT / ".venv" / ("Scripts/ruff.exe" if sys.platform == "win32" else "bin/ruff")
        results["lint_new_modules"] = execute([str(ruff), "check", *LINT_MODULES])
        results["measurements"] = measure()
        partial_passed = (
            results["tests"]["exit_code"] == 0 and results["lint_new_modules"]["exit_code"] == 0
        )
        if args.coverage:
            results["coverage_export"] = execute(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    ".tmp-context-learning-coverage.json",
                ]
            )
            if results["coverage_export"]["exit_code"] != 0:
                partial_passed = False
            else:
                try:
                    results["changed_line_coverage"] = changed_line_coverage(
                        ROOT / ".tmp-context-learning-coverage.json"
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    results["changed_line_coverage"] = {"passed": False, "error": str(error)}
                partial_passed = partial_passed and results["changed_line_coverage"]["passed"]
    if acceptance:
        mypy = ROOT / ".venv" / ("Scripts/mypy.exe" if sys.platform == "win32" else "bin/mypy")
        results["types_new_modules"] = execute([str(mypy), *NEW_MODULES, "--follow-imports=silent"])
        partial_passed &= results["types_new_modules"]["exit_code"] == 0
        if partial_passed:
            results["full_suite_runs"] = [
                execute(
                    [
                        sys.executable,
                        "-m",
                        "coverage",
                        "run",
                        "--source=novacode_cli",
                        "-m",
                        "pytest",
                        "tests",
                        "-q",
                        "--timeout=120",
                        "--junitxml=.tmp-context-learning-full-1.xml",
                    ]
                )
            ]
            partial_passed &= results["full_suite_runs"][0]["exit_code"] == 0
            results["coverage_export"] = execute(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    ".tmp-context-learning-coverage.json",
                ]
            )
            if results["coverage_export"]["exit_code"]:
                partial_passed = False
            else:
                try:
                    results["changed_line_coverage"] = changed_line_coverage(
                        ROOT / ".tmp-context-learning-coverage.json"
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    results["changed_line_coverage"] = {"passed": False, "error": str(error)}
                partial_passed &= results["changed_line_coverage"]["passed"]
        # Expensive and source-mutating gates run only after fast failures clear.
        # Every omitted check remains explicitly unavailable and acceptance fails.
        if partial_passed:
            results["mutations"] = mutations()
            partial_passed &= all(case["killed"] for case in results["mutations"])
        if partial_passed:
            results["shuffled_affected"] = execute(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *TARGETS,
                    "-q",
                    "--timeout=120",
                    "--random-order-seed=42",
                    "--random-order-bucket=global",
                ]
            )
            partial_passed &= results["shuffled_affected"]["exit_code"] == 0
        if partial_passed:
            run = execute(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "--timeout=120",
                    "--junitxml=.tmp-context-learning-full-2.xml",
                ]
            )
            results["full_suite_runs"].append(run)
            partial_passed &= run["exit_code"] == 0
    results["final_source_tree_sha256"] = source_hash()
    results["source_unchanged"] = (
        results["final_source_tree_sha256"] == results["source_tree_sha256"]
    )
    partial_passed &= results["source_unchanged"]
    results["partial_checks_passed"] = partial_passed
    results["passed"] = acceptance_verdict(results, complete_mode=acceptance)
    (ROOT / args.output).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if results["passed"] or (not acceptance and partial_passed) else 1


if __name__ == "__main__":
    raise SystemExit(main())

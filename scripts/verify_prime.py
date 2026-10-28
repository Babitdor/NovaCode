"""Reproduce Prime's offline contract and isolated evidence-boundary mutation checks."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ["tests/test_prime.py", "tests/test_prime_records.py", "-q"]
BOOTSTRAP = """
import importlib.abc
import importlib.util
import sys
class Override(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'novacode_cli.prime':
            return importlib.util.spec_from_file_location(fullname, sys.argv[1])
sys.meta_path.insert(0, Override())
import pytest
raise SystemExit(pytest.main(sys.argv[2:]))
"""
MUTATIONS = (
    (
        "accept observations started before definition",
        'if observation["sequence"] <= check["defined_at"]:',
        "if False:",
        "tests/test_prime_records.py::test_evidence_requires_definition_real_observation_and_correct_order",
    ),
    (
        "ignore exit-code and output assertions",
        'passed = execution["exit_code"] == check["expected_exit"] and all(',
        "passed = True or all(",
        "tests/test_prime_records.py::test_mechanical_assertions_fail_closed",
    ),
    (
        "accept evidence from another conversation thread",
        'and values.get("thread_id") == self.report["thread"]',
        "and True",
        "tests/test_prime.py::test_root_binding_rejects_other_threads_and_subgraphs",
    ),
)


def main() -> int:
    """Run the real contract first; each mutant must fail an assertion, never collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    baseline = subprocess.run([sys.executable, "-m", "pytest", *TESTS], cwd=ROOT, check=False)
    if baseline.returncode or not args.mutations:
        return baseline.returncode
    source = (ROOT / "novacode_cli/prime.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="nova-prime-mutations-") as temporary:
        target = Path(temporary) / "prime.py"
        for label, before, after, scenario in MUTATIONS:
            if source.count(before) != 1:
                print(f"FAIL: mutation source changed: {label}", file=sys.stderr)
                return 1
            target.write_text(source.replace(before, after, 1), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", BOOTSTRAP, str(target), scenario, "-q"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            output = result.stdout + result.stderr
            assertion_failed = "AssertionError" in output or "DID NOT RAISE ValueError" in output
            if result.returncode != 1 or not assertion_failed:
                print(f"FAIL: mutant survived or did not execute: {label}", file=sys.stderr)
                print(result.stdout + result.stderr, file=sys.stderr)
                return 1
            print(f"KILLED: {label}")
    print(
        f"{len(MUTATIONS)}/{len(MUTATIONS)} evidence-boundary mutants killed; "
        "source files unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

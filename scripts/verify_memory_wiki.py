"""Reproduce memory wiki checks with temporary user profiles and no new dependencies."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FOCUSED = [
    "tests/test_memory_wiki.py",
    "tests/test_dream_handler.py",
    "tests/test_hermes/test_memory_tiers.py",
    "tests/test_memory_prompt_cache_order.py",
    "tests/test_memory_retrieval.py",
    "tests/test_memory_search.py",
    "tests/test_memory_tools.py",
    "tests/test_plan_approval_persistence.py",
    "tests/test_recent_plans.py",
    "tests/test_plan_auto_scope.py",
    "tests/test_cowork_broker_middleware.py",
    "tests/test_tui_app.py::test_tui_research_dream_native",
]


def main() -> int:
    """Run selected checks; any unexpected failure returns a nonzero exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="nova-wiki-profile-") as profile:
        env = {
            **os.environ,
            "USERPROFILE": profile,
            "HOME": profile,
            "PYTHONUTF8": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        if args.mutations:
            path = ROOT / "novacode_cli/memory/wiki.py"
            original = path.read_bytes()
            source = original.decode("utf-8")
            mutants = [
                (
                    "lost source body",
                    "updates[path] = body + _navigation",
                    'updates[path] = "" + _navigation',
                    "test_maintenance_indexes_links_and_preserves_sources",
                ),
                (
                    "missing change guard",
                    "if _read(path) != originals.get(path):",
                    "if False:",
                    "test_concurrent_write_is_not_overwritten",
                ),
                (
                    "empty link validator",
                    "return broken\n",
                    "return []\n",
                    "test_broken_links_reported_and_scoped_project_pages_reachable",
                ),
            ]
            for label, before, after, test in mutants:
                if source.count(before) != 1:
                    message = f"Mutation target changed: {label}"
                    raise RuntimeError(message)
                mutant_path = Path(profile) / "wiki_mutant.py"
                mutant_path.write_text(source.replace(before, after), encoding="utf-8")
                result = subprocess.run(  # noqa: S603 - fixed interpreter/test arguments
                    [
                        sys.executable,
                        str(ROOT / "scripts/memory_wiki/run_mutant.py"),
                        str(mutant_path),
                        f"tests/test_memory_wiki.py::{test}",
                    ],
                    cwd=ROOT,
                    env=env,
                    check=False,
                )
                if result.returncode != 1:
                    message = f"Mutant was not killed by an assertion: {label}"
                    raise RuntimeError(message)
                if path.read_bytes() != original:
                    message = "Shared source changed during isolated mutation checks"
                    raise RuntimeError(message)
                sys.stdout.write(f"KILLED: {label}\n")
                sys.stdout.flush()
            return 0
        prefix = [sys.executable, "-m", "pytest"]
        if args.coverage:
            prefix = [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--branch",
                "--include=*/memory/wiki.py,*/tools/memory_wiki_tools.py",
                "-m",
                "pytest",
            ]
        result = subprocess.run(  # noqa: S603 - fixed interpreter and repository test paths
            [
                *prefix,
                *(["tests/"] if args.full else FOCUSED),
                "-q",
                "--timeout=300",
                "-p",
                "no:cacheprovider",
            ],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if result.returncode or not args.coverage:
            return result.returncode
        return subprocess.run(
            [sys.executable, "-m", "coverage", "report", "--show-missing", "--fail-under=85"],
            cwd=ROOT,
            env=env,
            check=False,
        ).returncode


if __name__ == "__main__":
    sys.exit(main())

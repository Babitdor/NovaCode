"""Run Phase 4-5 checks in an isolated home without paid dispatches."""

from __future__ import annotations

# Standalone verifier with fixed commands and a disposable application home.
# ruff: noqa: INP001, S603
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FOCUSED = [
    "tests/test_phase45.py",
    "tests/test_subagent_model_roles.py",
    "tests/test_subagent_registration.py",
    "tests/test_subagent_hitl.py",
    "tests/test_subagent_resilience.py",
    "tests/test_refreshing_skills.py",
    "tests/test_compaction.py",
    "tests/test_compaction_tail.py",
    "tests/test_compaction_derived_label.py",
    "tests/test_compaction_replay_leak.py",
    "tests/test_compaction_trigger_sync.py",
    "tests/test_context_compaction_guard.py",
    "tests/test_skill_libraries_and_resolvers.py",
    "tests/test_skills_runtime.py",
    "tests/test_skills_upstream.py",
    "tests/test_optimization.py",
    "tests/test_memory_prompt_cache_order.py",
    "tests/test_pipe_mode.py",
    "tests/test_session_crash_recovery.py",
]


def main() -> int:
    """Run focused or full pytest checks, retaining an aggregate JUnit artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--tests", nargs="*", help="Override the focused test paths")
    parser.add_argument("--output", type=Path, default=Path("phase45-tests.xml"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # A unique subtree avoids pytest's inaccessible shared temp parent and keeps
    # no-git discovery tests outside this repository's ancestor chain.
    with tempfile.TemporaryDirectory(prefix="nova-phase45-home-") as home:
        env = dict(os.environ, HOME=home, USERPROFILE=home, NOVA_DISABLE_UPDATE_CHECK="1")
        for name in list(env):
            if name.endswith("_API_KEY") or name in {"LANGSMITH_TOKEN", "OPENAI_BASE_URL"}:
                env.pop(name)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--tb=short",
            "--timeout=60",
            f"--junitxml={args.output}",
            f"--basetemp={Path(home) / 'pytest'}",
        ]
        if not args.full:
            command += args.tests if args.tests is not None else FOCUSED
        return subprocess.run(command, cwd=root, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""Run three isolated mutation probes without editing the working tree."""

# ruff: noqa: T201 - Command-line verification report.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MUTANTS = {
    "entry-root": (
        "self.switch(destination)",
        "pass",
        "test_enter_exit_preserves_files_and_commits",
    ),
    "exit-root": (
        "self.switch(self.original)",
        "self.switch(self.root)",
        "test_enter_exit_preserves_files_and_commits",
    ),
    "parallel-batch": (
        "or len(calls) > 1",
        "",
        "test_transition_rejects_mixed_batch_and_concurrent_tools",
    ),
}
CHILD_ARG_COUNT = 2


def main() -> int:
    """Require each deliberately broken behavior to produce a test failure."""
    if len(sys.argv) == CHILD_ARG_COUNT:
        import pytest

        import novacode_cli.tools.worktree_tools as module

        old, new, test = MUTANTS[sys.argv[1]]
        source = Path(module.__file__).read_text(encoding="utf-8")
        if source.count(old) != 1:
            message = "Mutation target must occur exactly once"
            raise RuntimeError(message)
        # Execute only this repository's fixed source and fixed substitutions.
        exec(compile(source.replace(old, new), module.__file__, "exec"), module.__dict__)  # noqa: S102
        return int(pytest.main([f"tests/test_worktree_tools.py::{test}", "-q"]))
    for name in MUTANTS:
        result = subprocess.run(  # noqa: S603 - Fixed local verification module and mutant names.
            [sys.executable, "-m", "scripts.verify_worktree_tools", name], check=False
        )
        if result.returncode != 1:
            message = f"Mutant {name}: expected assertion failure, got {result.returncode}"
            raise RuntimeError(message)
        print(f"Killed mutant: {name}", flush=True)
    print("3/3 behavioral mutants killed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

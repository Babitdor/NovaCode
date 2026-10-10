#!/usr/bin/env python3
"""Run NovaCode on a benchmark.

    uv run python scripts/run.py tb2 -m opencode/deepseek-v4.1-flash -n 2
    uv run python scripts/run.py tb3 -m openai/gpt-5.5 -e modal -n 8
    uv run python scripts/run.py tb2 -m ... -i terminal-bench/fix-git

Everything after the benchmark name goes straight to `harbor run`
(-m model, -i task, -l limit, -n concurrency, -k attempts, -e environment, --ak key=value).
Results land in jobs/<benchmark>/<timestamp>/.
"""

import os
import subprocess
import sys
from pathlib import Path

# Harbor Hub names; verify with `harbor run -d <name> -a oracle --dry-run`.
BENCHMARKS = {
    "tb2": ["-d", "terminal-bench/terminal-bench-2"],  # Terminal-Bench 2.0, 89 tasks
    "tb2.1": ["-d", "terminal-bench/terminal-bench-2-1"],  # 89 tasks
    "tb3": ["-d", "terminal-bench/terminal-bench@1"],  # Terminal-Bench 3.0, 74 tasks
    "tb-latest": ["-d", "terminal-bench/terminal-bench@latest"],
    "nova": ["-p", "nova-tasks"],  # local 6-task sanity set
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in BENCHMARKS:
        print(__doc__)
        print("benchmarks:", ", ".join(BENCHMARKS))
        return 2
    name, rest = sys.argv[1], sys.argv[2:]
    cmd = [
        # The harbor beside this interpreter, not whichever one is first on PATH.
        str(Path(sys.executable).parent / "harbor"), "run", *BENCHMARKS[name],
        "-a", "deepagents_harbor:NovaCodeWrapper",
        "-o", f"jobs/{name}",
        "-y", *rest,
    ]  # fmt: skip
    # Harbor prints box-drawing characters; a cp1252 Windows pipe chokes on them.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.call(cmd, env=env, cwd=Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tell NovaCode's learning how each trial of a job was actually graded.

    python scripts/learn_outcomes.py jobs/learn-train/2026-10-11__01-23-45 .eval-home-learn

A session only sees its own checks; the benchmark's verifier runs afterwards and
often disagrees (an agent's own script printed "ALL PASS" on a task whose real
tests failed four of thirteen). This records each task's real result against its
project in the learning home, which retracts anything a failed task had helped
put in the shared pool. scripts/run.py calls it after a learning=on run.
"""

import json
import sys
from pathlib import Path

# Importing the package redirects the process home to NOVA_EVAL_HOME first.
import deepagents_harbor  # noqa: F401
from novacode_cli.hermes.memory_tiers import mark_project_outcome, project_memory_key

LEARNER = "Nova-eval-learner"  # the assistant id the wrapper uses for learning runs


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    job, home = Path(sys.argv[1]), Path(sys.argv[2])
    agent_dir = home / ".nova" / "agents" / LEARNER
    if not agent_dir.is_dir():
        print(f"no learning memory at {agent_dir}")
        return 1
    passed = failed = retracted = 0
    for trial in sorted(p for p in job.iterdir() if p.is_dir()):
        result = trial / "result.json"
        if not result.exists():
            continue
        task = (json.loads(result.read_text(encoding="utf-8")).get("task_name") or trial.name.split("__")[0])
        task = task.split("/")[-1]
        reward_file = trial / "verifier" / "reward.txt"
        ok = reward_file.exists() and float(reward_file.read_text().strip() or 0) >= 1.0
        retracted += mark_project_outcome(agent_dir, project_memory_key(None, task), ok)
        passed, failed = passed + ok, failed + (not ok)
    print(f"outcomes recorded: {passed} passed, {failed} failed; shared lessons retracted: {retracted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

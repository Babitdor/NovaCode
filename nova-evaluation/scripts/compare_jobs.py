"""Compare matched benchmark jobs without calling a model.

Usage: python scripts/compare_jobs.py jobs/tb2/<baseline> jobs/tb2/<candidate>
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Trial:
    task: str
    profile: dict
    reward: bool | None


def _load_job(job: Path) -> dict[str, _Trial]:
    if not job.is_dir():
        raise ValueError(f"job directory does not exist: {job}")
    trials: dict[str, _Trial] = {}
    for trial_dir in sorted(path for path in job.iterdir() if path.is_dir()):
        config_path = trial_dir / "config.json"
        if not config_path.exists():
            continue
        config = json.loads(config_path.read_text(encoding="utf-8"))
        task = config.get("task") or {}
        task_name = task.get("name") or task.get("path")
        if not task_name:
            raise ValueError(f"missing task ID: {config_path}")
        if task_name in trials:
            raise ValueError(f"duplicate task ID {task_name!r} in {job}")
        reward_path = trial_dir / "verifier" / "reward.txt"
        reward: bool | None = None
        if reward_path.exists():
            value = reward_path.read_text(encoding="utf-8").strip()
            if value not in {"0", "1"}:
                raise ValueError(f"invalid reward {value!r}: {reward_path}")
            reward = value == "1"
        agent = config.get("agent") or {}
        environment = config.get("environment") or {}
        profile = {
            "task_source": task.get("source"),
            "task_ref": task.get("ref"),
            "agent": agent.get("name"),
            "model": agent.get("model_name"),
            "agent_kwargs": agent.get("kwargs") or {},
            "environment": environment.get("type"),
        }
        missing = [key for key, value in profile.items() if value is None or value == ""]
        if missing:
            raise ValueError(f"missing evaluation profile fields {missing}: {config_path}")
        trials[task_name] = _Trial(task_name, profile, reward)
    return trials


def compare_jobs(baseline: Path, candidate: Path) -> list[tuple[str, bool | None, bool | None]]:
    """Return task-level reward pairs after checking their evaluation profiles."""
    before = _load_job(baseline)
    after = _load_job(candidate)
    shared = sorted(before.keys() & after.keys())
    if not shared:
        raise ValueError("no shared tasks between benchmark jobs")
    comparisons = []
    for task in shared:
        if before[task].profile != after[task].profile:
            differences = {
                key: (before[task].profile[key], after[task].profile[key])
                for key in before[task].profile
                if before[task].profile[key] != after[task].profile[key]
            }
            raise ValueError(f"evaluation profile mismatch for {task}: {differences}")
        comparisons.append((task, before[task].reward, after[task].reward))
    return comparisons


def main() -> int:
    """Print gains and regressions for comparable tasks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        comparisons = compare_jobs(args.baseline, args.candidate)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    for task, before, after in comparisons:
        label = (
            "gain"
            if before is False and after is True
            else ("regression" if before is True and after is False else "unchanged")
        )
        if before is None or after is None:
            label = "incomplete"
        print(f"{task}: {before} -> {after} ({label})")
    gains = sum(before is False and after is True for _, before, after in comparisons)
    regressions = sum(before is True and after is False for _, before, after in comparisons)
    print(f"Comparable tasks: {len(comparisons)}; gains: {gains}; regressions: {regressions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

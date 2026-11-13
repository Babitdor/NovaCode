"""Run balanced, isolated CI shards without changing pytest's timeout policy."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MANIFEST = Path(__file__).with_name("ci-regression-tests.json")


def partition(weights: dict[str, float], count: int) -> list[list[str]]:
    if count < 1 or count > len(weights):
        raise ValueError("Shard count must be between 1 and the number of selectors")
    if any(value <= 0 for value in weights.values()):
        raise ValueError("Test weights must be positive")
    groups: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for selector in sorted(weights, key=lambda key: (-weights[key], key)):
        index = min(range(count), key=lambda i: (totals[i], i))
        groups[index].append(selector)
        totals[index] += weights[selector]
    # Keep the original suite order inside each process.
    order = {selector: i for i, selector in enumerate(weights)}
    return [sorted(group, key=order.__getitem__) for group in groups]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--list", action="store_true", help="Print selectors without running")
    args = parser.parse_args()
    weights = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not 1 <= args.shard <= args.shards <= len(weights):
        parser.error("Expected 1 <= shard <= shards <= number of selectors")
    selected = partition(weights, args.shards)[args.shard - 1]
    if args.list:
        print("\n".join(selected))
        return 0
    results = Path("test-results")
    results.mkdir(exist_ok=True)
    label = f"regression-{args.shard}"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "--tb=short",
        "--durations=20",
        f"--junitxml={results / (label + '.xml')}",
        *selected,
    ]
    print(f"Running shard {args.shard}/{args.shards}: {len(selected)} selectors", flush=True)
    with (results / (label + ".log")).open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())

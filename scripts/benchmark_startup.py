"""Repeat isolated CLI launches using one interpreter and collect process-tree costs.

Application caches live in a disposable home; OS filesystem caches are uncontrolled.
The first sample has an empty application home; later samples reuse that home.
No credentials or provider calls are needed. Requires benchmark-only psutil.
"""

from __future__ import annotations

# Standalone measurement command; argv comes from fixed, local workloads.
# ruff: noqa: INP001, T201, S603
import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

_STARTUP_DEADLINE_SECONDS = 120


def launch(
    root: Path, home: Path, arguments: list[str], *, command: list[str] | None = None
) -> dict:
    """Drain both pipes while sampling the process tree from the parent."""
    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(root),
        PYTHONIOENCODING="utf-8",
        NOVA_DISABLE_UPDATE_CHECK="1",
        NOVA_BENCHMARK_ROOT=str(root),
    )
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        started = time.perf_counter()
        process = subprocess.Popen(
            command or [sys.executable, "-m", "novacode_cli", *arguments],
            cwd=root,
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        peak = 0
        cpu_by_pid = {}
        while process.poll() is None:
            try:
                parent = psutil.Process(process.pid)
                members = [parent, *parent.children(recursive=True)]
                rss = 0
                for member in members:
                    try:
                        rss += member.memory_info().rss
                        cpu = member.cpu_times()
                        cpu_by_pid[member.pid] = cpu.user + cpu.system
                    except psutil.Error:
                        pass
                peak = max(peak, rss)
            except psutil.Error:
                pass
            if time.perf_counter() - started > _STARTUP_DEADLINE_SECONDS:
                process.kill()
                process.wait()
                message = "CLI startup exceeded 120 seconds"
                raise TimeoutError(message)
            time.sleep(0.005)
        elapsed = time.perf_counter() - started
        stdout.seek(0)
        stderr.seek(0)
        output, errors = stdout.read(), stderr.read()
        return {
            "wall_ms": elapsed * 1000,
            "cpu_ms": sum(cpu_by_pid.values()) * 1000,
            "peak_tree_mib": peak / 1024**2,
            "exit_code": process.returncode,
            "stdout_sha256": hashlib.sha256(output).hexdigest(),
            "stderr": errors.decode("utf-8", errors="replace")[-2000:],
            "result": json.loads(output) if command and process.returncode == 0 else None,
        }


def main() -> None:
    """Write raw samples as well as median and nearest-rank p95."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nova-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--import-profiles", type=Path, help="Separate, unmeasured importtime logs")
    parser.add_argument("--suite", choices=("startup", "ui", "pipe", "local"), default="startup")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    if args.import_profiles and args.suite != "startup":
        parser.error("import profiling is supported by the startup suite")
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "interpreter": sys.executable,
        "nova_root": str(args.nova_root.resolve()),
        "filesystem_cache": "uncontrolled",
        "sample_interval_ms": 5,
        "workloads": {},
    }
    harness = Path(__file__).resolve().parent
    workloads = [("version", ["--version"]), ("help", ["--help"])]
    if args.suite == "pipe":
        workloads = [("pipe", [sys.executable, str(harness / "benchmark_pipe.py")])]
    if args.suite == "local":
        workloads = [
            (
                "local",
                [
                    sys.executable,
                    str(harness.parent / "nova-evaluation" / "scripts" / "local_ab.py"),
                    "--nova-root",
                    str(args.nova_root.resolve()),
                    "--local-only",
                    "--repetitions",
                    "1",
                ],
            )
        ]
    if args.suite == "ui":
        workloads = [
            (
                f"tabs-{count}",
                [
                    sys.executable,
                    str(harness / "benchmark_tabs.py"),
                    "--tabs",
                    str(count),
                    "--events",
                    "2000",
                    "--history",
                    "100",
                ],
            )
            for count in (1, 4)
        ]
        workloads += [
            (
                f"idle-{count}",
                [
                    sys.executable,
                    str(harness / "benchmark_idle.py"),
                    "--sessions",
                    str(count),
                    "--seconds",
                    "1",
                ],
            )
            for count in (1, 4)
        ]
    for name, flags in workloads:
        with tempfile.TemporaryDirectory(prefix="nova-startup-") as directory:
            samples = [
                launch(
                    args.nova_root.resolve(),
                    Path(directory),
                    flags,
                    command=flags if args.suite != "startup" else None,
                )
                for _ in range(args.repetitions)
            ]
        metrics = {}
        for key in ("wall_ms", "cpu_ms", "peak_tree_mib"):
            ordered = sorted(sample[key] for sample in samples)
            metrics[key] = {
                "median": statistics.median(ordered),
                "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
            }
        report["workloads"][name] = {
            "samples": samples,
            "metrics": metrics,
            "first_sample_application_cache": "empty",
            "later_samples_application_cache": "reused",
        }
        if args.suite == "local" and all(sample["result"] for sample in samples):
            local_metrics = {}
            for workload in samples[0]["result"]["workloads"]:
                values = [sample["result"]["workloads"][workload][0] for sample in samples]
                local_metrics[workload] = {"passed": all(value["passed"] for value in values)}
                for metric in ("wall_ms", "cpu_ms", "loop_delay_max_ms"):
                    if metric in values[0]:
                        ordered = sorted(value[metric] for value in values)
                        local_metrics[workload][metric] = {
                            "median": statistics.median(ordered),
                            "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
                        }
            report["workloads"][name]["local_metrics"] = local_metrics
        print(name, metrics, flush=True)
        if args.import_profiles:
            args.import_profiles.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="nova-imports-") as directory:
                env = dict(
                    os.environ,
                    HOME=directory,
                    USERPROFILE=directory,
                    PYTHONPATH=str(args.nova_root.resolve()),
                    PYTHONIOENCODING="utf-8",
                    NOVA_DISABLE_UPDATE_CHECK="1",
                )
                with (args.import_profiles / f"{name}.txt").open("w", encoding="utf-8") as log:
                    subprocess.run(
                        [sys.executable, "-X", "importtime", "-m", "novacode_cli", *flags],
                        cwd=args.nova_root.resolve(),
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=log,
                        check=False,
                        timeout=_STARTUP_DEADLINE_SECONDS,
                    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

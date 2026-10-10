"""Run Nova in-process on a local nova-task; report outcome, model calls, tokens.

No Docker, no Harbor: the task's ``environment/`` is staged into a temp dir that
becomes the workspace root, Nova's own agent (full middleware stack, the user's
configured model) runs the instruction with approvals off, then the task's own
pytest suite grades the result. Point ``--nova-root`` at two checkouts (e.g. a
worktree of the baseline commit and the working tree) to A/B a change.

    python local_ab.py --nova-root <checkout> --task fix-data-processing-bug

Only tasks whose files ship in ``environment/`` work here; ones that build
state in their Dockerfile (git-bisect-regression, ...) need the Harbor run.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parents[1] / "nova-tasks"


def _stage(task: str, root: Path) -> str:
    """Copy the task's files into ``root`` and return the adapted instruction."""
    shutil.copytree(
        TASKS_DIR / task / "environment",
        root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("Dockerfile"),
    )
    # The tests hard-code the container layout: /app and a `python3` binary.
    for test in (root / "tests").rglob("*.py"):
        text = test.read_text(encoding="utf-8")
        text = text.replace('"/app', f'"{root.as_posix()}').replace('"python3"', "sys.executable")
        if "sys.executable" in text and "import sys" not in text:
            text = "import sys\n" + text
        test.write_text(text, encoding="utf-8")
    # In the instruction /app is the project root, which is "/" to Nova.
    instruction = (TASKS_DIR / task / "instruction.md").read_text(encoding="utf-8")
    return re.sub(r"/app/?", "/", instruction)


async def _run(nova_root: Path, task: str) -> dict:
    sys.path.insert(0, str(nova_root))
    from langchain_core.messages import AIMessage
    from novacode_cli.agents.core_agent import create_agent_with_config
    from novacode_cli.config.model_create import create_model
    from novacode_cli.memory.store import get_async_durable_store
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    work = Path(tempfile.mkdtemp(prefix=f"nova-ab-{task}-"))
    instruction = _stage(task, work)

    agent, _ = create_agent_with_config(
        model=create_model(),
        assistant_id="nova-ab-eval",  # one memory dir for all eval runs
        tools=[],
        auto_approve=True,
        workspace_root=str(work),
        store=await get_async_durable_store(),  # as the Harbor wrapper does
    )
    started = time.monotonic()
    error = None
    config = {"recursion_limit": 200, "configurable": {"thread_id": uuid.uuid4().hex}}
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": instruction}]}, config=config
        )
        messages = result["messages"]
    except Exception as exc:  # noqa: BLE001 — a crashed run is a result, not a harness error
        error = f"{type(exc).__name__}: {exc}"
        # Still count what it spent: the checkpointer holds the partial run.
        messages = (await agent.aget_state(config)).values.get("messages", [])
    elapsed = time.monotonic() - started

    ai = [m for m in messages if isinstance(m, AIMessage)]
    usage = [m.usage_metadata or {} for m in ai]
    graded = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return {
        "task": task,
        "nova_root": str(Path(sys.modules["novacode_cli"].__file__).parent.parent),
        "passed": graded.returncode == 0,
        "tests": (graded.stdout.strip().splitlines() or [""])[-1],
        "model_calls": len(ai),
        "tool_calls": sum(len(m.tool_calls or []) for m in ai),
        "wrote_todos": any(tc["name"] == "write_todos" for m in ai for tc in m.tool_calls or []),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "seconds": round(elapsed, 1),
        "error": error,
        "workdir": str(work),
    }


async def _run_local(nova_root: Path, repetitions: int) -> dict:
    """Exercise actual local backends and restored context without any provider."""
    sys.path.insert(0, str(nova_root))
    from novacode_cli.backends.filesystem import (
        OptimizedFilesystemBackend,
        OptimizedLocalShellBackend,
    )
    from novacode_cli.session.adapters import HarnessMessage, ImportedSession
    from novacode_cli.session.imported_context import ImportedContext

    report = {"python": sys.version, "nova_root": str(nova_root), "workloads": {}}
    with tempfile.TemporaryDirectory(prefix="nova-local-") as directory:
        root = Path(directory)
        for name, count in (("small", 100), ("large", 5000)):
            repo = root / name
            for index in range(count):
                folder = repo / "src" / str(index // 100)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"file{index}.py").write_text(
                    f"def function_{index}():\n    return 'benchmark_needle'\n", encoding="utf-8"
                )
            (repo / "日本語.py").write_text("line\n" * 1000000, encoding="utf-8")
            backend = OptimizedFilesystemBackend(root_dir=str(repo), virtual_mode=True)
            for operation in ("grep", "read"):
                samples = []
                for _ in range(repetitions):
                    delays = []
                    running = True

                    async def ticker():
                        while running:
                            before = time.perf_counter()
                            await asyncio.sleep(0.005)
                            delays.append(max(0, time.perf_counter() - before - 0.005) * 1000)

                    tick = asyncio.create_task(ticker())
                    started, cpu = time.perf_counter(), time.process_time()
                    if operation == "grep":
                        result = await backend.agrep("benchmark_needle", path="/", glob="*.py")
                        outcome = not result.error and len(result.matches) == count
                    else:
                        result = await backend.aread("/日本語.py", offset=900000, limit=20)
                        outcome = (
                            not result.error
                            and result.start_line == 900001
                            and result.end_line == 900020
                        )
                    elapsed = (time.perf_counter() - started) * 1000
                    cpu_ms = (time.process_time() - cpu) * 1000
                    running = False
                    await tick
                    samples.append(
                        {
                            "wall_ms": elapsed,
                            "cpu_ms": cpu_ms,
                            "loop_delay_max_ms": max(delays, default=0),
                            "passed": bool(outcome),
                        }
                    )
                report["workloads"][f"{name}-{operation}"] = samples
        shell = OptimizedLocalShellBackend(root_dir=str(root), virtual_mode=True, inherit_env=True)
        (root / "noisy.py").write_text(
            "import sys\nsys.stdout.write('x'*1000000)\nsys.stderr.write('e'*1000000)\nsys.exit(17)\n",
            encoding="utf-8",
        )
        samples = []
        for _ in range(repetitions):
            started, cpu = time.perf_counter(), time.process_time()
            result = await shell.aexecute(f'"{sys.executable}" noisy.py')
            samples.append(
                {
                    "wall_ms": (time.perf_counter() - started) * 1000,
                    "cpu_ms": (time.process_time() - cpu) * 1000,
                    "passed": result.exit_code == 17,
                    "output_chars": len(result.output),
                }
            )
        report["workloads"]["noisy-subprocess"] = samples
        context = ImportedContext(
            ImportedSession(
                "fixture",
                "fixture",
                str(root),
                [HarnessMessage("user", "Evidence and tool results. " * 100) for _ in range(500)],
            ),
            "relevant",
        )
        samples = []
        for _ in range(repetitions):
            started, cpu = time.perf_counter(), time.process_time()
            result = await asyncio.to_thread(context.build, 12000)
            samples.append(
                {
                    "wall_ms": (time.perf_counter() - started) * 1000,
                    "cpu_ms": (time.process_time() - cpu) * 1000,
                    "passed": bool(result),
                }
            )
        report["workloads"]["restored-context"] = samples
    report["metrics"] = {}
    for name, samples in report["workloads"].items():
        report["metrics"][name] = {}
        for key in ("wall_ms", "cpu_ms"):
            values = sorted(sample[key] for sample in samples)
            report["metrics"][name][key] = {
                "median": statistics.median(values),
                "p95": values[math.ceil(len(values) * 0.95) - 1],
            }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nova-root", required=True, type=Path)
    parser.add_argument("--task")
    parser.add_argument(
        "--local-only", action="store_true", help="Synthetic workloads; no model requests"
    )
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.local_only and not args.task:
        parser.error("--task is required for live runs")
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    profile = cProfile.Profile() if args.profile else None
    if profile:
        profile.enable()
    report = asyncio.run(
        _run_local(args.nova_root.resolve(), args.repetitions)
        if args.local_only
        else _run(args.nova_root.resolve(), args.task)
    )
    if profile:
        profile.disable()
        args.profile.parent.mkdir(parents=True, exist_ok=True)
        profile.dump_stats(args.profile)
    line = json.dumps(report)
    print(line)
    if args.out:
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()

"""Offline paired deterministic workloads; no provider calls or credentials.

Run from the workspace: python scripts/benchmark_phase45.py --output phase45.json
"""

from __future__ import annotations

# Standalone benchmark; output contains aggregate measurements only.
# ruff: noqa: INP001, T201, ANN001, ANN201, ANN202, D103, ARG001, ARG005, PLR2004
import argparse
import asyncio
import cProfile
import json
import math
import os
import pstats
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novacode_cli import computation_cache as cache
from novacode_cli.agents.agent_file import _split
from novacode_cli.skills.load import _list_dir


async def measure(work, count):
    delays = []
    running = True

    async def ticker():
        while running:
            start = time.perf_counter()
            await asyncio.sleep(0.001)
            delays.append(max(0, time.perf_counter() - start - 0.001) * 1000)

    task = asyncio.create_task(ticker())
    samples, cpu = [], []
    profile = cProfile.Profile()
    for i in range(count):
        await asyncio.sleep(0)
        started, cpu_start = time.perf_counter(), time.process_time()
        await asyncio.to_thread(profile.runcall, work, i)
        samples.append((time.perf_counter() - started) * 1000)
        cpu.append((time.process_time() - cpu_start) * 1000)
    running = False
    await task
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
        "samples_ms": samples,
        "cpu_median_ms": statistics.median(cpu),
        "event_loop_delay_p95_ms": sorted(delays)[math.ceil(len(delays) * 0.95) - 1],
        "cache": cache.snapshot(),
        "profile_top": [
            {"function": key[2], "calls": value[1], "cpu_seconds": value[2]}
            for key, value in sorted(
                pstats.Stats(profile).stats.items(), key=lambda item: item[1][2], reverse=True
            )[:10]
        ],
    }


def section_experiment(path, start, end):
    """Benchmark-only expandable excerpt with path and line provenance."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return {
        "path": str(path),
        "start_line": start,
        "end_line": min(end, len(lines)),
        "text": "\n".join(lines[start - 1 : end]),
        "expandable": True,
        "full_read_available": True,
    }


async def main(args):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.messages.utils import count_tokens_approximately
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from novacode_cli.config.config import _find_project_root

    with tempfile.TemporaryDirectory(prefix="nova-phase45-") as temporary:
        directory = Path(temporary)
        for i in range(80):
            skill = directory / f"skill-{i}"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                f"---\nname: skill-{i}\ndescription: Process source safely {i}\n---\nBody\n",
                encoding="utf-8",
            )
        definitions = [
            f"---\nname: agent-{i}\ntools: [read_file, edit_file]\n"
            f"skill_names: [code]\n---\nReview source {i}\n"
            for i in range(80)
        ]
        messages = [
            HumanMessage(content="Preserve instructions"),
            AIMessage(
                content="", tool_calls=[{"name": "read", "args": {"path": "文件.py"}, "id": "1"}]
            ),
            ToolMessage(content="source " * 1000, tool_call_id="1"),
        ]

        def sample(path: str) -> str:
            """Read the requested path."""
            return path

        tool = StructuredTool.from_function(sample)

        def parse(i):
            for content in definitions:
                _split(content)
                _split(content)

        def changed_parse(i):
            for content in definitions:
                _split(content + str(i))

        def changed_skills(i):
            (directory / "skill-0" / "SKILL.md").write_text(
                f"---\nname: skill-0\ndescription: Changed {i}\n---\nBody", encoding="utf-8"
            )
            _list_dir(directory)

        workloads = {
            "agent_parse_unchanged": parse,
            "agent_parse_changed": changed_parse,
            "skills_unchanged": lambda i: _list_dir(directory),
            "skills_changed": changed_skills,
            "tool_schema": lambda i: [convert_to_openai_tool(tool) for _ in range(80)],
            "token_count": lambda i: [count_tokens_approximately(messages) for _ in range(80)],
            "request_serialization": lambda i: [
                json.dumps([m.model_dump(mode="json") for m in messages]) for _ in range(80)
            ],
            "repository_discovery": lambda i: [_find_project_root() for _ in range(80)],
        }
        results = {}
        old_disabled = os.environ.get("NOVA_DISABLE_COMPUTATION_CACHE")
        try:
            for name, work in workloads.items():
                results[name] = {}
                for mode in ("baseline", "candidate"):
                    cache.clear()
                    os.environ["NOVA_DISABLE_COMPUTATION_CACHE"] = (
                        "1" if mode == "baseline" else "0"
                    )
                    work(-1)  # imports and indexes outside measurement
                    results[name][mode] = await measure(work, args.repetitions)
        finally:
            if old_disabled is None:
                os.environ.pop("NOVA_DISABLE_COMPUTATION_CACHE", None)
            else:
                os.environ["NOVA_DISABLE_COMPUTATION_CACHE"] = old_disabled
        output = {
            "python": sys.version,
            "repetitions": args.repetitions,
            "live_calls": 0,
            "pricing": "unknown",
            "workloads": results,
            "context_sections_enabled": args.context_sections,
        }
        if args.context_sections:
            excerpt = section_experiment(Path(__file__), 1, 20)
            output["context_section_experiment"] = {
                "lines": excerpt["end_line"] - excerpt["start_line"] + 1,
                "expandable": excerpt["expandable"],
                "production_enabled": False,
            }
        args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
        for name, modes in results.items():
            before, after = modes["baseline"]["median_ms"], modes["candidate"]["median_ms"]
            print(f"{name}: {before:.3f} -> {after:.3f} ms ({(1 - after / before) * 100:.1f}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("phase45-measurements.json"))
    parser.add_argument(
        "--context-sections",
        action="store_true",
        help="Internal offline section experiment; never changes production reads",
    )
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("at least two repetitions required")
    asyncio.run(main(args))

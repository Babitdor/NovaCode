"""Measure offline background-monitoring latency and event-loop stalls.

Run with the same Python environment before and after a change. Uses real SDK
client construction with fake status responses; sends no network requests and
does not load user agent files or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from weakref import WeakSet

if TYPE_CHECKING:
    from langgraph_sdk.client import LangGraphClient, RunsClient

sys.path.insert(0, os.environ.get("NOVA_BENCHMARK_ROOT", str(Path(__file__).resolve().parents[1])))


async def measure(count: int) -> dict[str, float | int]:
    """Measure setup/teardown of concurrent completed task observations."""
    from langgraph_sdk import get_client
    from psutil import Process

    from novacode_cli.remote import async_task_watcher

    clients: WeakSet[LangGraphClient] = WeakSet()
    client_constructions = 0
    discovery_calls = 0

    def discover() -> list[dict[str, str]]:
        nonlocal discovery_calls
        discovery_calls += 1
        time.sleep(0.1)  # Reproducible slow filesystem/configuration lookup.
        return [{"name": "benchmark", "url": "http://127.0.0.1:2024"}]

    async def status(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "success"}

    def build(*, url: str | None = None, headers: dict[str, str] | None = None) -> LangGraphClient:
        nonlocal client_constructions
        client = get_client(url=url, headers=headers, api_key=None)
        client.runs = cast("RunsClient", SimpleNamespace(get=status))
        clients.add(client)
        client_constructions += 1
        return client

    delays = []
    process = Process()
    peak_rss = process.memory_info().rss
    finished = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal peak_rss
        previous = time.perf_counter()
        while not finished.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            delays.append(max(0, now - previous - 0.005) * 1000)
            previous = now
            peak_rss = max(peak_rss, process.memory_info().rss)

    with (
        patch("langgraph_sdk.get_client", build),
        patch(
            "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
            discover,
        ),
        patch.object(async_task_watcher, "_POLL_INTERVAL", 0),
    ):
        watcher = async_task_watcher.AsyncTaskWatcher(lambda *_args: None)
        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        started = time.perf_counter()
        cpu_started = time.process_time()
        try:
            watcher.sync_from_state(
                {
                    str(index): {
                        "status": "running",
                        "agent_name": "benchmark",
                        "thread_id": str(index),
                        "run_id": str(index),
                    }
                    for index in range(count)
                }
            )
            while watcher._tasks or watcher._pending:
                await asyncio.gather(*tuple(watcher._tasks))
            await watcher.aclose()
            elapsed = (time.perf_counter() - started) * 1000
            cpu_ms = (time.process_time() - cpu_started) * 1000
        finally:
            await watcher.aclose()
            finished.set()
            await ticker
            # Also clean up when benchmarking revisions with incomplete cleanup.
            for client in clients:
                await client.aclose()
    return {
        "tasks": count,
        "client_constructions": client_constructions,
        "discovery_calls": discovery_calls,
        "total_ms": round(elapsed, 2),
        "cpu_ms": round(cpu_ms, 2),
        "peak_rss_mib": round(peak_rss / (1024 * 1024), 2),
        "max_loop_stall_ms": round(max(delays, default=0), 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, choices=range(1, 257), default=64)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args.tasks)), indent=2))  # noqa: T201 — benchmark result.

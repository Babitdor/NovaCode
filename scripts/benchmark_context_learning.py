"""Offline observational cache, discovery, session, and snapshot measurements."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from langgraph.store.memory import InMemoryStore

from novacode_cli.hermes.tracker import ToolUsageTracker
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware


def corpus_measure(count, size):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        memories = root / "memories"
        memories.mkdir()
        for index in range(count):
            (memories / f"lesson-{index}.md").write_text("observational lesson " * (size // 21))
        middleware = AgentMemoryMiddleware.__new__(AgentMemoryMiddleware)
        middleware.agent_dir = root
        middleware._project_key = "fixture"
        middleware._corpus_cache = None
        middleware._corpus_sig = None
        start = time.perf_counter()
        corpus = middleware._load_memory_corpus()
        # Score all topics as real retrieval does; discovery alone does not
        # populate the parsed-body cache.
        for _, value in corpus.items():
            assert value[1]
        cold = time.perf_counter() - start
        start = time.perf_counter()
        middleware._load_memory_corpus()
        warm = time.perf_counter() - start
        cache = middleware._parsed_memory_cache
        return {"files": count, "approximate_file_bytes": size, "cold_ms": cold * 1000,
                "warm_discovery_ms": warm * 1000, "cache_entries": len(cache),
                "cache_resident_bytes": cache.resident_bytes,
                "entry_limit": cache.max_entries, "byte_limit": cache.max_bytes}


async def concurrent_measure():
    store = InMemoryStore()
    trackers = [ToolUsageTracker(store, owner=f"fixture-{index}") for index in range(8)]
    async def episode(tracker):
        for _ in range(100):
            await tracker.increment_counter()
            await tracker.record_tool_usage("execute", True, evidence_source="fixture")
    start = time.perf_counter()
    await asyncio.gather(*(episode(tracker) for tracker in trackers))
    return {"sessions": 8, "episodes_per_session": 100,
            "counts": [await tracker.get_call_count() for tracker in trackers],
            "duration_ms": (time.perf_counter() - start) * 1000}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="context-learning-benchmarks.json")
    args = parser.parse_args()
    results = {"live_provider_calls": 0, "recall": "observational; usefulness not demonstrated",
               "small_corpus": corpus_measure(32, 1024), "large_corpus": corpus_measure(2000, 8192),
               "concurrent_sessions": asyncio.run(concurrent_measure())}
    from verify_context_learning import measure
    results["context_overhead"] = measure()
    Path(args.output).write_text(json.dumps(results, indent=2))
    for corpus in (results["small_corpus"], results["large_corpus"]):
        if corpus["cache_entries"] > 512 or corpus["cache_resident_bytes"] > 16 * 1024 * 1024:
            return 1
    return 0 if results["concurrent_sessions"]["counts"] == [100] * 8 else 1


if __name__ == "__main__":
    raise SystemExit(main())

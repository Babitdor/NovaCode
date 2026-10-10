"""Bounded, process-local reuse of deterministic JSON values.

Only serialized bytes are retained; callers always receive defensive copies.
No tools, permissions, model objects, or provider responses belong here.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import sys
from collections import OrderedDict
from contextlib import suppress
from threading import RLock
from typing import cast

ENTRY_LIMIT = 256
CACHE_BYTES = 8 * 1024 * 1024
PROCESS_BYTES = 32 * 1024 * 1024
_LOCK = RLock()
_ENTRIES: OrderedDict[tuple[str, str], bytes] = OrderedDict()
_STATS: dict[str, dict[str, int]] = {}


def digest(content: str | bytes) -> str:
    """Hash content without retaining it as a cache key."""
    return hashlib.sha256(
        content.encode("utf-8") if isinstance(content, str) else content
    ).hexdigest()


def _event(namespace: str, outcome: str) -> None:
    stats = _STATS.setdefault(namespace, {})
    stats[outcome] = stats.get(outcome, 0) + 1
    if os.environ.get("NOVA_LOCAL_METRICS") == "1":
        with suppress(OSError, ValueError):
            sys.stderr.write(
                json.dumps(
                    {
                        "nova_cache_metrics": {
                            "cache": namespace,
                            "outcome": outcome,
                            "entries": len(_ENTRIES),
                            "retained_bytes": sum(map(len, _ENTRIES.values())),
                            "validation_ns": stats.get("validation_ns", 0),
                            "computation_ns": stats.get("computation_ns", 0),
                        }
                    }
                )
                + "\n"
            )


def record_timing(namespace: str, phase: str, elapsed_ns: int) -> None:
    """Accumulate numeric validation/computation time without retaining inputs."""
    with _LOCK:
        stats = _STATS.setdefault(namespace, {})
        stats[phase + "_ns"] = stats.get(phase + "_ns", 0) + elapsed_ns


def get(namespace: str, key: str) -> object | None:
    """Read a defensive copy, or miss when explicitly disabled."""
    with _LOCK:
        if os.environ.get("NOVA_DISABLE_COMPUTATION_CACHE") == "1":
            return None
        payload = _ENTRIES.get((namespace, key))
        _event(namespace, "miss" if payload is None else "hit")
        if payload is None:
            return None
        _ENTRIES.move_to_end((namespace, key))
        return cast("object", json.loads(payload))


def put(namespace: str, key: str, value: object) -> None:
    """Retain JSON data within both namespace and shared process budgets."""
    if os.environ.get("NOVA_DISABLE_COMPUTATION_CACHE") == "1":
        return
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        # JSON must preserve the value exactly (e.g. reject tuples/non-string keys).
        if json.loads(payload) != value:
            return
    except (TypeError, ValueError):
        return
    if len(payload) > CACHE_BYTES:
        return
    with _LOCK:
        _ENTRIES.pop((namespace, key), None)
        _ENTRIES[namespace, key] = payload
        while True:
            own = [k for k in _ENTRIES if k[0] == namespace]
            if len(own) > ENTRY_LIMIT or sum(len(_ENTRIES[k]) for k in own) > CACHE_BYTES:
                victim = own[0]
            elif sum(map(len, _ENTRIES.values())) > PROCESS_BYTES:
                victim = next(iter(_ENTRIES))
            else:
                break
            del _ENTRIES[victim]
            _event(victim[0], "eviction")


def clear(namespace: str | None = None) -> None:
    """Explicit configuration/workspace invalidation and process teardown."""
    with _LOCK:
        for key in list(_ENTRIES):
            if namespace is None or key[0] == namespace:
                del _ENTRIES[key]
        if namespace is None:
            _STATS.clear()
        else:
            _STATS.pop(namespace, None)


def snapshot() -> dict[str, object]:
    """Content-free counters and actual retained serialized payload size."""
    with _LOCK:
        return {
            "entries": len(_ENTRIES),
            "retained_bytes": sum(map(len, _ENTRIES.values())),
            "caches": {name: dict(stats) for name, stats in _STATS.items()},
        }


atexit.register(clear)

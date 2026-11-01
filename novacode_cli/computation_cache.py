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
_NAMESPACES: dict[str, OrderedDict[str, None]] = {}
_BYTES: dict[str, int] = {}
_RETAINED_BYTES = 0
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
                            "retained_bytes": _RETAINED_BYTES,
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
        _NAMESPACES[namespace].move_to_end(key)
    # Immutable bytes remain valid across eviction; decoding a defensive copy
    # must not hold the shared lock while other sessions access their caches.
    return cast("object", json.loads(payload))


def _remove(namespace: str, key: str) -> None:
    """Update both LRU orders and byte accounting while the caller holds the lock."""
    global _RETAINED_BYTES  # noqa: PLW0603
    payload = _ENTRIES.pop((namespace, key))
    _RETAINED_BYTES -= len(payload)
    _BYTES[namespace] -= len(payload)
    del _NAMESPACES[namespace][key]
    if not _NAMESPACES[namespace]:
        del _NAMESPACES[namespace]
        del _BYTES[namespace]


def put(namespace: str, key: str, value: object) -> None:
    """Retain JSON data within both namespace and shared process budgets."""
    global _RETAINED_BYTES  # noqa: PLW0603
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
        if (namespace, key) in _ENTRIES:
            _remove(namespace, key)
        _ENTRIES[namespace, key] = payload
        _NAMESPACES.setdefault(namespace, OrderedDict())[key] = None
        _BYTES[namespace] = _BYTES.get(namespace, 0) + len(payload)
        _RETAINED_BYTES += len(payload)
        while namespace in _NAMESPACES and (
            len(_NAMESPACES[namespace]) > ENTRY_LIMIT or _BYTES[namespace] > CACHE_BYTES
        ):
            _remove(namespace, next(iter(_NAMESPACES[namespace])))
            _event(namespace, "eviction")
        while _RETAINED_BYTES > PROCESS_BYTES:
            victim_namespace, victim_key = next(iter(_ENTRIES))
            _remove(victim_namespace, victim_key)
            _event(victim_namespace, "eviction")


def clear(namespace: str | None = None) -> None:
    """Explicit configuration/workspace invalidation and process teardown."""
    with _LOCK:
        keys = (
            list(_ENTRIES)
            if namespace is None
            else [(namespace, key) for key in _NAMESPACES.get(namespace, {})]
        )
        for owner, key in keys:
            _remove(owner, key)
        if namespace is None:
            _STATS.clear()
        else:
            _STATS.pop(namespace, None)


def snapshot() -> dict[str, object]:
    """Content-free counters and actual retained serialized payload size."""
    with _LOCK:
        return {
            "entries": len(_ENTRIES),
            "retained_bytes": _RETAINED_BYTES,
            "caches": {
                name: {
                    **_STATS.get(name, {}),
                    "entries": len(_NAMESPACES.get(name, {})),
                    "retained_bytes": _BYTES.get(name, 0),
                }
                for name in dict.fromkeys([*_STATS, *_NAMESPACES])
            },
        }


atexit.register(clear)

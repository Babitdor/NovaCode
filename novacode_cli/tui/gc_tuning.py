"""Garbage-collector settings for an interactive UI.

The TUI and the agent share one event loop, so a GC pause is a UI freeze.
CPython's defaults (700, 10, 10) suit short scripts: a full (generation-2)
collection walks every tracked object, and it comes round after only ~70,000
allocations. Measured in the TUI with nothing but widgets loaded (303k
objects): 19 full collections in a 12-second run, up to 122 ms each, 11% of
wall time. With an agent, its tools and a long conversation loaded the heap is
several times larger and the pauses scale with it — the freeze watchdog's
multi-second stalls with ``_weakrefset._remove`` on top of the stack were this.

Two changes:

* Higher thresholds, so young collections are fewer and a full one is rare
  (same run: no full collections, 2% of wall time, worst pause 32 ms).
* A deliberate full collection when the terminal loses focus — the one moment
  a pause is invisible. Rate-limited, and skipped while a turn is running.

Reference cycles are still collected; only the *timing* moves. Freezing after
mounting would put the app/widget cycles into a permanent generation and retain
closed apps indefinitely. Objects freed by reference counting are unaffected.
"""

from __future__ import annotations

import gc
import sys
import time

#: (young, middle, old) collection thresholds. See the module docstring.
THRESHOLDS = (10_000, 20, 100)

#: Minimum seconds between idle full collections.
IDLE_COLLECT_INTERVAL = 120.0

_last_full = 0.0


def tune() -> bool:
    """Apply the interactive settings. Call once, after start-up. Returns whether applied.

    A no-op under pytest: the settings are process-global and would leak into
    every later test.
    """
    if "pytest" in sys.modules:
        return False
    gc.collect()
    gc.set_threshold(*THRESHOLDS)
    return True


def collect_while_idle(*, busy: bool = False) -> bool:
    """Run a full collection if one is due and nothing is waiting on the UI."""
    global _last_full
    now = time.monotonic()
    if busy or now - _last_full < IDLE_COLLECT_INTERVAL:
        return False
    _last_full = now
    gc.collect()
    return True


__all__ = ["IDLE_COLLECT_INTERVAL", "THRESHOLDS", "collect_while_idle", "tune"]

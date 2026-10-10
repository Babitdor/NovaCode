"""Content-free auxiliary dispatch records and purpose propagation."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

request_purpose: ContextVar[str] = ContextVar("nova_request_purpose", default="unknown")
task_correlation: ContextVar[str | None] = ContextVar("nova_task_correlation", default=None)


@contextmanager
def correlate(task_id: str) -> Iterator[None]:
    """Bind an opaque task identity without emitting a synthetic request."""
    token = task_correlation.set(task_id)
    try:
        yield
    finally:
        task_correlation.reset(token)


@contextmanager
def dispatch(purpose: str, *, task_id: str | None = None) -> Iterator[None]:
    """Observe an actual dispatch, independently of any SDK/HTTP observations."""
    token = request_purpose.set(purpose)
    task_token = task_correlation.set(task_id or task_correlation.get())
    started = time.perf_counter()
    outcome = "error"
    try:
        yield
        outcome = "success"
    finally:
        if os.environ.get("NOVA_LOCAL_METRICS") == "1":
            record = {
                "kind": "auxiliary_dispatch",
                "purpose": purpose,
                "task_id": task_correlation.get(),
                "dispatch_count": 1,
                "sdk_runs": None,
                "http_attempts": None,
                "duration_ms": (time.perf_counter() - started) * 1000,
                "outcome": outcome,
                "estimated_cost_usd": None,
                "pricing_status": "unknown",
            }
            with suppress(OSError, ValueError):
                sys.stderr.write(json.dumps({"nova_local_metrics": record}) + "\n")
        task_correlation.reset(task_token)
        request_purpose.reset(token)

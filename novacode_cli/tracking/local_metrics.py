"""Opt-in content-free model/tool timings on stderr (NOVA_LOCAL_METRICS=1).

These measurements are diagnostics, not a billing ledger. In particular an SDK
retry can contain several provider requests behind one LangChain model run.
"""

from __future__ import annotations

# Framework callback payloads intentionally follow LangChain's flexible types.
# ruff: noqa: ANN401, ARG002
import json
import os
import sys
import time
from collections import OrderedDict
from threading import RLock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from novacode_cli.tracking.request_metrics import request_purpose, task_correlation
from novacode_cli.tracking.usage_tree import _extract_usage, _scope_stack

_MAX_ACTIVE_RUNS = 2048


class LocalMetricsCallback(BaseCallbackHandler):
    """Measure concurrent runs without retaining prompts or generated content."""

    def __init__(self) -> None:
        """Create a bounded table shared by concurrent callbacks."""
        super().__init__()
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._lock = RLock()

    def _start(
        self,
        kind: str,
        serialized: dict | None,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: dict | None = None,
    ) -> None:
        if os.environ.get("NOVA_LOCAL_METRICS") != "1":
            return
        serialized = serialized or {}
        kwargs = serialized.get("kwargs") or {}
        metadata = metadata or {}
        with self._lock:
            if str(run_id) in self._runs:
                return
            self._runs[str(run_id)] = {
                "kind": kind,
                "run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id is not None else None,
                "provider": str(metadata.get("ls_provider") or ""),
                "node": str(metadata.get("langgraph_node") or ""),
                "scope": list(_scope_stack.get()),
                "task_id": str(
                    metadata.get("nova_task_id")
                    or task_correlation.get()
                    or parent_run_id
                    or run_id
                ),
                "purpose": str(
                    metadata.get("nova_request_purpose")
                    or (request_purpose.get() if request_purpose.get() != "unknown" else None)
                    or metadata.get("langgraph_node")
                    or "unknown"
                ),
                "sdk_runs": 1 if kind == "model" else 0,
                "http_attempts": None,
                "estimated_cost_usd": None,
                "pricing_status": "unknown",
                "model": str(
                    metadata.get("ls_model_name")
                    or kwargs.get("model")
                    or kwargs.get("model_name")
                    or serialized.get("name")
                    or ""
                ),
                "started": time.perf_counter(),
                "first_token_ms": None,
            }
            if len(self._runs) > _MAX_ACTIVE_RUNS:
                self._runs.popitem(last=False)

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        """Start timing a chat-model run without retaining its messages."""
        self._start("model", serialized, run_id, parent_run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict,
        prompts: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        """Start timing a completion-model run without retaining its prompts."""
        self._start("model", serialized, run_id, parent_run_id, metadata)

    def on_llm_new_token(self, token: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Record the first nonempty token callback."""
        with self._lock:
            run = self._runs.get(str(run_id))
            if run is not None and run["first_token_ms"] is None and token:
                run["first_token_ms"] = (time.perf_counter() - run["started"]) * 1000

    def _finish(self, run_id: Any, outcome: str, usage: dict | None = None) -> None:
        with self._lock:
            run = self._runs.pop(str(run_id), None)
            if run is None:
                return
            run["duration_ms"] = (time.perf_counter() - run.pop("started")) * 1000
            run["outcome"] = outcome
            if usage is not None:
                run["usage"] = usage
            try:
                sys.stderr.write(json.dumps({"nova_local_metrics": run}) + "\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass  # diagnostics must not break execution when stderr closes

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Emit timing and provider-normalized usage for a completed run."""
        self._finish(run_id, "success", _extract_usage(response))

    def on_llm_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Release timing state and emit a failed model outcome."""
        self._finish(run_id, "error")

    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Start timing an inherited graph tool call."""
        self._start("tool", serialized, run_id, parent_run_id)

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Emit a completed tool outcome without its result."""
        self._finish(run_id, "success")

    def on_tool_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Release timing state and emit a failed tool outcome."""
        self._finish(run_id, "error")


_HANDLER = LocalMetricsCallback()


def callbacks() -> list[BaseCallbackHandler]:
    """Return the shared observer only when explicitly enabled."""
    return [_HANDLER] if os.environ.get("NOVA_LOCAL_METRICS") == "1" else []

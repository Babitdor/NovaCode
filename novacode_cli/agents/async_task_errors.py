# ruff: noqa: INP001 - agents uses namespace packaging
"""Recover failed async-run diagnostics from LangGraph's checkpoint tasks."""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _with_error(run: dict[str, Any], state: dict[str, Any], run_id: str) -> dict[str, Any]:
    # A resumed thread may already have a newer checkpoint from a different run.
    if (state.get("metadata") or {}).get("run_id") != run_id:
        return run
    from novacode_cli.security.prompt_injection import _credential_values, _redact
    from novacode_cli.session.history_export import redact

    secrets = _credential_values()
    errors = []
    for task in state.get("tasks") or []:
        error = task.get("error")
        if error:
            errors.append(redact(_redact(str(error), secrets)))
    if not errors:
        return run
    return {**run, "error": "\n".join(errors)[:4000]}


def bind_run_errors(client: Any) -> Any:  # noqa: ANN401
    """Enrich runs.get only when the server omitted a failed run's error.

    LangGraph's Run schema has no error field; errors live in ThreadState.tasks.
    Keep deepagents' normal check tool and state updates by filling that gap at
    its existing client-factory seam. Never fetch or return child transcripts.
    """
    runs: Any = getattr(client, "runs", None)
    original = getattr(runs, "get", None)
    if original is None or getattr(original, "_nova_errors_bound", False) is True:
        return client

    def needs_error(run: dict[str, Any]) -> bool:
        return run.get("status") == "error" and not run.get("error")

    if inspect.iscoroutinefunction(original):

        async def aget(thread_id: str, run_id: str, **kwargs: Any) -> Any:  # noqa: ANN401
            run = await original(thread_id, run_id, **kwargs)
            if needs_error(run):
                try:
                    state = await client.threads.get_state(thread_id=thread_id)
                    return _with_error(run, state, run_id)
                except Exception:  # noqa: BLE001 - status remains usable if state is unavailable
                    logger.debug("Failed to fetch async error checkpoint for run %s", run_id)
            return run

        replacement: Any = aget
    else:

        def get(thread_id: str, run_id: str, **kwargs: Any) -> Any:  # noqa: ANN401
            run = original(thread_id, run_id, **kwargs)
            if needs_error(run):
                try:
                    state = client.threads.get_state(thread_id=thread_id)
                    return _with_error(run, state, run_id)
                except Exception:  # noqa: BLE001 - status remains usable if state is unavailable
                    logger.debug("Failed to fetch async error checkpoint for run %s", run_id)
            return run

        replacement = get

    replacement._nova_errors_bound = True
    runs.get = replacement
    return client

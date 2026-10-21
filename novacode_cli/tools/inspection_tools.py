"""Feature-gated context metrics and managed terminal-panel text capture."""

from __future__ import annotations

import os
from collections import Counter
from typing import Annotated

from langchain.tools import ToolRuntime, tool
from pydantic import Field


def feature_enabled(name: str) -> bool:
    """Accept either the exact gate name or its NOVA_ prefixed equivalent."""
    return os.environ.get(f"NOVA_{name}", os.environ.get(name, "")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@tool("CtxInspectTool")
def inspect_context(runtime: ToolRuntime) -> dict:
    """Inspect context token estimates, message counts and the last request snapshot.

    Returns metrics only; this never changes, collapses or exposes message content.
    Requires CONTEXT_COLLAPSE.
    """
    if not feature_enabled("CONTEXT_COLLAPSE"):
        return {"error": "CONTEXT_COLLAPSE is disabled."}
    from langchain_core.messages.utils import count_tokens_approximately

    messages = runtime.state.get("messages", [])
    snapshot = runtime.state.get("_context_snapshot") or {}
    keys = (
        "model",
        "estimated_input_tokens",
        "estimated_text_tokens",
        "estimated_system_tokens",
        "estimated_schema_tokens",
        "estimated_multimodal_tokens",
        "mcp_tools",
        "mcp_schema_tokens",
        "output_reservation",
        "confidence",
        "schema_count",
    )
    result = {
        "message_count": len(messages),
        "messages_by_type": dict(Counter(getattr(m, "type", "unknown") for m in messages)),
        "conversation_token_estimate": count_tokens_approximately(messages),
        "request_snapshot": {k: snapshot[k] for k in keys if k in snapshot},
    }
    if snapshot.get("model"):
        from novacode_cli.context import ContextManager

        window = ContextManager(str(snapshot["model"]), use_dynamic=False).window_size()
        result["context_window"] = window
        used = snapshot.get("estimated_input_tokens")
        if isinstance(used, int):
            result["remaining_input_tokens"] = max(
                0, window - used - int(snapshot.get("output_reservation") or 0)
            )
    return result


@tool("TerminalCaptureTool")
def capture_terminal(
    panel_id: str,
    tail_lines: Annotated[int, Field(ge=1, le=1000)] = 100,
    max_chars: Annotated[int, Field(ge=128, le=50000)] = 12000,
) -> dict:
    """Capture text from a Nova-managed terminal task panel, including finished tasks.

    panel_id is the task ID from list_background_tasks. Captures bounded retained
    output, not another application's terminal or a screenshot. Requires TERMINAL_PANEL.
    """
    if not feature_enabled("TERMINAL_PANEL"):
        return {"error": "TERMINAL_PANEL is disabled."}
    from rich.text import Text

    from novacode_cli.shell.jobs import get_registry

    job = get_registry().resolve(panel_id)
    if job is None:
        return {"error": f"Unknown terminal panel {panel_id!r}. Use list_background_tasks."}
    text = Text.from_ansi(job.output).plain
    lines = text.splitlines()
    tail = "\n".join(lines[-tail_lines:])
    return {
        "panel_id": job.task_id,
        "status": job.status,
        "exit_code": job.exit_code,
        "text": tail[-max_chars:],
        "retained_lines": len(lines),
        "truncated": len(lines) > tail_lines or len(tail) > max_chars,
    }


def build_inspection_tools() -> list:
    """Disabled gates add no schemas or discovery entries."""
    return [
        t
        for gate, t in (("CONTEXT_COLLAPSE", inspect_context), ("TERMINAL_PANEL", capture_terminal))
        if feature_enabled(gate)
    ]

"""Headless (non-interactive) agent runner.

Drives a single prompt through the UI-agnostic event stream
(:func:`novacode_cli.core.agent_loop.iterate_agent_events`) to completion with
no human present: tool approvals are auto-resolved, output is formatted for
machines (see :mod:`novacode_cli.headless.output`), the session is auto-saved,
and a meaningful exit code is returned.

Exit codes: ``0`` success, ``1`` error during execution, ``2`` max-turns hit.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from novacode_cli import ui_events as ev
from novacode_cli.config.config import console, settings
from novacode_cli.core.agent_loop import (
    default_interrupt_response,
    iterate_agent_events,
)
from novacode_cli.headless.output import HeadlessOutput
from novacode_cli.ui.hitl_approval import evaluate_tool_actions
from novacode_cli.core.input_preparation import build_agent_config
from novacode_cli.utils.model_info import get_current_provider

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MAX_TURNS = 2


def _resolve_interrupt(event: ev.InterruptRequest, session_state, *, deny_tools: bool) -> None:
    """Resolve a human-in-the-loop interrupt without a human.

    Tool interrupts auto-approve (honouring the policy/plan-mode gate via
    :func:`evaluate_tool_actions`, which short-circuits to all-approve when
    ``auto_approve`` is set) unless ``deny_tools`` forces a fail-closed reject.
    Question/plan interrupts resolve to the benign default — there is nobody to
    answer them. Always resolves in ``finally`` so the agent loop never hangs.
    """
    try:
        if event.kind == "tool" and not deny_tools:
            payload = event.payload if isinstance(event.payload, dict) else {}
            decisions = evaluate_tool_actions(
                payload,
                session_state,
                plan_mode_enabled=getattr(session_state, "plan_mode_enabled", False),
            )
            # A None verdict means "ask the user" — impossible headless, so
            # approve (auto_approve is forced on for non-deny headless runs).
            decisions = [d if d is not None else {"type": "approve"} for d in decisions]
            any_rejected = any((d or {}).get("type") == "reject" for d in decisions)
            event.future.set_result({"decisions": decisions, "any_rejected": any_rejected})
        else:
            event.future.set_result(default_interrupt_response(event.kind))
    finally:
        if not event.future.done():
            event.future.set_result(default_interrupt_response(event.kind))


async def run_headless(  # noqa: PLR0912, PLR0915 — single linear event loop
    *,
    agent,
    assistant_id: str | None,
    session_state,
    backend=None,
    model_name: str | None = None,
    session_manager=None,
) -> int:
    """Run one headless prompt to completion and return an exit code."""
    prompt: str = session_state.headless_prompt
    fmt: str = getattr(session_state, "headless_output_format", "text")
    max_turns: int | None = getattr(session_state, "headless_max_turns", None)
    deny_tools: bool = getattr(session_state, "headless_deny_tools", False)

    # Write results to the fd-1 dup captured before agent build (survives stdio
    # MCP servers closing sys.stdout); falls back to stdout under tests.
    out_fd = getattr(session_state, "headless_out_fd", None)
    out = HeadlessOutput(fmt, session_state.session_id, model_name, fd=out_fd)
    out.init()

    started = time.monotonic()
    num_turns = 0
    in_tool_round = False
    text_parts: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    subtype = "success"
    is_error = False

    # Artifacts persist per session (headless -c/--resume reuses the id).
    try:
        from novacode_cli.artifacts.registry import bind_session

        bind_session(
            getattr(session_state, "session_id", "") or "",
            getattr(session_manager, "sessions_dir", None),
        )
    except Exception:  # noqa: BLE001 — never fail a run on artifact restore
        pass

    source = iterate_agent_events(
        prompt,
        agent,
        assistant_id,
        session_state,
        backend=backend,
        seen_message_ids=set(),
    )

    try:
        async for event in source:
            # --- max-turns accounting (a turn = one model step) ---------
            if isinstance(event, ev.ToolCall):
                if not in_tool_round:
                    num_turns += 1
                    in_tool_round = True
            elif isinstance(event, ev.AssistantMessage):
                num_turns += 1
                in_tool_round = False
                text_parts.append(event.text)

            out.handle_event(event)

            if isinstance(event, ev.InterruptRequest):
                _resolve_interrupt(event, session_state, deny_tools=deny_tools)
                continue

            if isinstance(event, ev.UsageUpdate):
                usage["input_tokens"] = max(usage["input_tokens"], event.input_tokens)
                usage["output_tokens"] = max(usage["output_tokens"], event.output_tokens)
                usage["cache_read_tokens"] = max(
                    usage["cache_read_tokens"], event.cache_read_tokens
                )
                usage["cache_creation_tokens"] = max(
                    usage["cache_creation_tokens"], event.cache_creation_tokens
                )
                continue

            if isinstance(event, ev.Error):
                is_error = True
                subtype = "error_during_execution"
                if not text_parts and event.message:
                    text_parts.append(event.message)
                break

            if isinstance(event, ev.Cancelled):
                is_error = True
                subtype = "error_during_execution"
                break

            if isinstance(event, ev.Done):
                break

            # Past the budget? Stop before the next model step begins.
            if max_turns is not None and num_turns > max_turns:
                is_error = True
                subtype = "error_max_turns"
                break
    except Exception as exc:  # noqa: BLE001 — headless must never crash uncaught
        is_error = True
        subtype = "error_during_execution"
        if not text_parts:
            text_parts.append(f"{type(exc).__name__}: {exc}")
        console.print(f"[red]Headless run failed: {type(exc).__name__}: {exc}[/red]")
    finally:
        with contextlib.suppress(Exception):
            await source.aclose()

    duration_ms = int((time.monotonic() - started) * 1000)
    result_text = "\n".join(p for p in text_parts if p).strip()

    # Emitting the result and autosaving must never raise — an exception here
    # would skip the caller's os._exit() and leave non-daemon MCP/sqlite threads
    # blocking interpreter shutdown (a hang). Fail to an error exit code instead.
    try:
        out.result(
            subtype=subtype,
            is_error=is_error,
            result_text=result_text,
            num_turns=num_turns,
            duration_ms=duration_ms,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001
        is_error = True
        console.print(f"[red]Headless output failed: {type(exc).__name__}: {exc}[/red]")

    await _autosave(
        agent=agent,
        assistant_id=assistant_id,
        session_state=session_state,
        model_name=model_name,
        session_manager=session_manager,
        is_error=is_error,
    )

    if subtype == "error_max_turns":
        return EXIT_MAX_TURNS
    return EXIT_ERROR if is_error else EXIT_OK


async def _autosave(
    *,
    agent,
    assistant_id: str | None,
    session_state,
    model_name: str | None,
    session_manager,
    is_error: bool,
) -> None:
    """Best-effort persist of the headless run as a resumable session."""
    if session_manager is None or not assistant_id:
        return
    try:
        config = build_agent_config(session_state.thread_id, assistant_id)
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return
        todos = state.values.get("todos") or getattr(session_state, "todos", None)
        session_manager.save_session(
            session_id=session_state.session_id,
            thread_id=session_state.thread_id,
            messages=messages,
            assistant_id=assistant_id,
            todos=todos,
            model_name=model_name,
            model_provider=get_current_provider(),
            project_root=settings.project_root or Path.cwd(),
            task_status="failed" if is_error else "completed",
        )
    except Exception as exc:  # noqa: BLE001 — never fail the run on save
        console.print(f"[dim]Headless session save skipped: {exc}[/dim]")

"""Headless (non-interactive) agent runner.

Drives a single prompt through the UI-agnostic event stream
(:func:`novacode_cli.core.agent_loop.iterate_agent_events`) to completion with
no human present: tool approvals are auto-resolved, output is formatted for
machines (see :mod:`novacode_cli.headless.output`), the session is auto-saved,
and a meaningful exit code is returned.

Exit codes: 0 success, 1 error, 2 turn limit, 3 human input required,
124 timeout, 130 cancellation, 141 closed output pipe.
"""

from __future__ import annotations

import contextlib
import asyncio
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
EXIT_APPROVAL_REQUIRED = 3
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
EXIT_BROKEN_PIPE = 141


def _diagnostic(message):
    # MCP shutdown can close a Python stream; logging must not hide a result.
    with contextlib.suppress(Exception):
        console.print(message, markup=False)


async def _resolve_interrupt(
    event: ev.InterruptRequest, session_state, *, deny_tools: bool
) -> bool:
    """Resolve a human-in-the-loop interrupt without a human.

    Tool interrupts follow :func:`evaluate_tool_actions`, including optional
    decision-model review for auto-approve sessions, unless ``deny_tools``
    forces a fail-closed reject.
    Question/plan interrupts resolve to the benign default — there is nobody to
    answer them. Always resolves in ``finally`` so the agent loop never hangs.
    """
    try:
        if event.kind == "tool" and not deny_tools:
            payload = event.payload if isinstance(event.payload, dict) else {}
            decisions = await asyncio.to_thread(
                evaluate_tool_actions,
                payload,
                session_state,
                plan_mode_enabled=getattr(session_state, "plan_mode_enabled", False),
                user_request=getattr(session_state, "headless_prompt", "") or "",
            )
            needs_human = any(d is None for d in decisions)
            # No human can answer: an unresolved verdict grants nothing.
            decisions = [
                d
                if d is not None
                else {
                    "type": "reject",
                    "message": "Approval requires an interactive session or explicit --auto-approve.",
                }
                for d in decisions
            ]
            any_rejected = any((d or {}).get("type") == "reject" for d in decisions)
            if not event.future.done():
                event.future.set_result({"decisions": decisions, "any_rejected": any_rejected})
            return needs_human
        else:
            if not event.future.done():
                event.future.set_result(default_interrupt_response(event.kind))
            return event.kind in ("question", "plan")
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
    out = HeadlessOutput(
        fmt,
        session_state.session_id,
        model_name,
        fd=out_fd,
        include_partial_messages=getattr(session_state, "headless_include_partial_messages", False),
    )

    started = time.monotonic()
    num_turns = 0
    in_tool_round = False
    main_tool_calls = set()
    text_parts: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    subtype = "success"
    is_error = False
    exit_code = EXIT_OK
    timeout = getattr(session_state, "headless_timeout", None)
    deadline = getattr(session_state, "headless_deadline", None)
    if deadline is None and timeout is not None:
        deadline = started + timeout
    completed = False
    needs_human = False

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

    async def consume():
        nonlocal num_turns, in_tool_round, is_error, subtype, exit_code, completed, needs_human
        out.init()
        async for event in source:
            # --- max-turns accounting (a turn = one model step) ---------
            if isinstance(event, ev.ToolCall) and event.is_main_agent:
                if not in_tool_round:
                    num_turns += 1
                    in_tool_round = True
                if event.call_id is not None:
                    main_tool_calls.add(event.call_id)
            elif isinstance(event, ev.ToolResult):
                if event.call_id in main_tool_calls:
                    main_tool_calls.remove(event.call_id)
                    if not main_tool_calls:
                        in_tool_round = False
                elif event.call_id is None and not main_tool_calls:
                    in_tool_round = False
            elif isinstance(event, ev.AssistantMessage) and not event.is_subagent:
                num_turns += 1
                in_tool_round = False

            # Check before resolving another approval or accepting Done.
            if max_turns is not None and num_turns > max_turns:
                is_error, subtype, exit_code = True, "error_max_turns", EXIT_MAX_TURNS
                if isinstance(event, ev.InterruptRequest) and not event.future.done():
                    event.future.set_result(default_interrupt_response(event.kind))
                break

            if isinstance(event, ev.AssistantMessage) and not event.is_subagent:
                text_parts.append(event.text)
            out.handle_event(event)

            if isinstance(event, ev.InterruptRequest):
                needs_human |= bool(
                    await _resolve_interrupt(event, session_state, deny_tools=deny_tools)
                )
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
                exit_code = EXIT_ERROR
                if not text_parts and event.message:
                    text_parts.append(event.message)
                break

            if isinstance(event, ev.Cancelled):
                is_error = True
                if deadline is not None and time.monotonic() >= deadline:
                    subtype, exit_code = "error_timeout", EXIT_TIMEOUT
                else:
                    subtype, exit_code = "error_cancelled", EXIT_CANCELLED
                break

            if isinstance(event, ev.Done):
                completed = True
                if needs_human:
                    is_error, subtype, exit_code = (
                        True,
                        "error_approval_required",
                        EXIT_APPROVAL_REQUIRED,
                    )
                break

    budget = asyncio.timeout(None if deadline is None else max(0, deadline - time.monotonic()))
    try:
        async with budget:
            await consume()
        if not completed and not is_error:
            is_error, subtype, exit_code = True, "error_incomplete_stream", EXIT_ERROR
    except TimeoutError as error:
        is_error = True
        if budget.expired() or (deadline is not None and time.monotonic() >= deadline):
            subtype, exit_code = "error_timeout", EXIT_TIMEOUT
        else:
            subtype, exit_code = "error_during_execution", EXIT_ERROR
            if not text_parts:
                text_parts.append(f"TimeoutError: {error}")
    except asyncio.CancelledError:
        is_error = True
        if deadline is not None and time.monotonic() >= deadline:
            subtype, exit_code = "error_timeout", EXIT_TIMEOUT
        else:
            subtype, exit_code = "error_cancelled", EXIT_CANCELLED
    except BrokenPipeError:
        return EXIT_BROKEN_PIPE
    except Exception as exc:  # noqa: BLE001 — headless must never crash uncaught
        is_error = True
        subtype = "error_during_execution"
        exit_code = EXIT_ERROR
        if not text_parts:
            text_parts.append(f"{type(exc).__name__}: {exc}")
        _diagnostic(f"Headless run failed: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(source.aclose(), timeout=2)

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
            exit_code=exit_code,
        )
        session_state.headless_result_emitted = True
        session_state.headless_exit_code = exit_code
    except BrokenPipeError:
        return EXIT_BROKEN_PIPE
    except Exception as exc:  # noqa: BLE001
        is_error = True
        exit_code = EXIT_ERROR
        _diagnostic(f"Headless output failed: {type(exc).__name__}: {exc}")

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            _autosave(
                agent=agent,
                assistant_id=assistant_id,
                session_state=session_state,
                model_name=model_name,
                session_manager=session_manager,
                is_error=is_error,
            ),
            timeout=5,
        )

    return exit_code


async def run_bootstrap(awaitable, session_state):
    """Include initialization in the deadline and emit structured startup errors."""
    started = time.monotonic()
    timeout = getattr(session_state, "headless_timeout", None)
    if timeout is not None:
        session_state.headless_deadline = started + timeout
    budget = asyncio.timeout(timeout)
    try:
        async with budget:
            await awaitable
        return
    except TimeoutError as error:
        if budget.expired():
            code, subtype, message = EXIT_TIMEOUT, "error_timeout", "Headless deadline exceeded"
        else:
            code, subtype, message = (
                EXIT_ERROR,
                "error_during_initialization",
                f"TimeoutError: {error}",
            )
    except asyncio.CancelledError:
        code, subtype, message = EXIT_CANCELLED, "error_cancelled", "Headless run cancelled"
    except Exception as error:
        code, subtype, message = (
            EXIT_ERROR,
            "error_during_initialization",
            f"{type(error).__name__}: {error}",
        )
    except SystemExit:
        code, subtype, message = (
            EXIT_ERROR,
            "error_during_initialization",
            "Nova initialization ended without a result; see stderr",
        )
    if getattr(session_state, "headless_result_emitted", False):
        # Once the terminal result is flushed, teardown cannot replace it with
        # another result or an inconsistent process exit code.
        _diagnostic(f"Headless cleanup interrupted: {message}")
        return
    session_state.headless_exit_code = code
    if not getattr(session_state, "headless_result_emitted", False):
        try:
            HeadlessOutput(
                session_state.headless_output_format,
                session_state.session_id,
                None,
                fd=getattr(session_state, "headless_out_fd", None),
            ).result(
                subtype=subtype,
                is_error=True,
                result_text=message,
                num_turns=0,
                duration_ms=int((time.monotonic() - started) * 1000),
                usage={},
                exit_code=code,
            )
        except Exception as error:
            session_state.headless_exit_code = (
                EXIT_BROKEN_PIPE if isinstance(error, BrokenPipeError) else EXIT_ERROR
            )
    _diagnostic(message)


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
        await asyncio.to_thread(
            session_manager.save_session,
            session_id=session_state.session_id,
            thread_id=session_state.thread_id,
            messages=messages,
            assistant_id=assistant_id,
            todos=todos,
            model_name=model_name,
            model_provider=getattr(session_state, "session_model_provider", None) or get_current_provider(),
            project_root=settings.project_root or Path.cwd(),
            task_status="failed" if is_error else "completed",
        )
    except Exception as exc:  # noqa: BLE001 — never fail the run on save
        _diagnostic(f"Headless session save skipped: {exc}")

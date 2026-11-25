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
from typing import Any

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

#: How long a one-shot run waits for background tasks its turn started
#: (Claude-Code-style wait-at-exit). Override with NOVA_HEADLESS_BG_WAIT_SECONDS;
#: 0 disables the wait entirely.
BG_WAIT_DEFAULT_SECONDS = 600.0


def _load_output_schema(session_state) -> tuple[dict | None, str | None]:
    """Resolve ``--output-schema`` (inline JSON or a path) into a schema dict.

    Returns ``(schema, error)`` — exactly one is set. An invalid schema is a
    structured command-style failure before any model call.
    """
    raw = getattr(session_state, "headless_output_schema", None)
    if not raw:
        return None, None
    import json
    import jsonschema

    try:
        schema = json.loads(raw)
    except (ValueError, TypeError):
        from pathlib import Path as _Path

        try:
            schema = json.loads(_Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, f"--output-schema is not valid JSON or a readable file: {error}"
    if not isinstance(schema, dict):
        return None, "--output-schema must be a JSON object"
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as error:
        return None, f"--output-schema is not a valid JSON Schema: {error.message}"
    return schema, None


def _structured_output(result_text: str, schema: dict) -> tuple[bool, Any | None]:
    """Validate the final answer against the schema.

    Returns ``(ok, parsed)``. The answer must be a bare JSON object (fences
    are tolerated); anything else fails validation and the result simply omits
    ``structured_output`` — matching Claude Code's behavior.
    """
    import json
    import jsonschema

    text = result_text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return False, None
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError:
        return False, None
    return True, parsed


def _prompt_with_schema(prompt: str, schema: dict) -> str:
    """Instruct the model to answer with schema-valid JSON."""
    import json as _json

    requirement = (
        "[OUTPUT REQUIREMENT] Your FINAL response must be exactly one JSON object"
        " that validates against this JSON Schema — no markdown fences, no prose:"
        f"\n{_json.dumps(schema)}"
    )
    return f"{prompt}\n\n{requirement}"


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
            decisions = (
                [None for _ in payload.get("action_requests", [])]
                if payload.get("requires_owner_approval")
                else await asyncio.to_thread(
                    evaluate_tool_actions,
                    payload,
                    session_state,
                    plan_mode_enabled=getattr(session_state, "plan_mode_enabled", False),
                    user_request=getattr(session_state, "headless_prompt", "") or "",
                )
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


async def _dispatch_command(
    prompt: str,
    agent,
    session_state,
    assistant_id: str | None,
    model_name: str | None,
    session_manager,
) -> tuple[bool, str, str | None, bool]:
    """Dispatch a `/slash-command` through the shared registry.

    Returns ``(ok, captured_output, kickoff_prompt, wants_exit)``. A handler
    that returns a string produced a kickoff prompt (e.g. `/goal <objective>`
    primes the goal and returns what the TUI would send to the agent next);
    ``exit`` asks the caller to end the run.
    """
    from novacode_cli.commands.commands import _get_registry, handle_command
    from novacode_cli.ui.ui_elements import TokenTracker

    cmd = prompt.strip().lstrip("/").split(maxsplit=1)[0].lower()
    if _get_registry().get(cmd) is None:
        return (
            False,
            f"Unknown command: /{cmd} — run `nova commands --json` for the available surface",
            None,
            False,
        )
    try:
        with console.capture() as capture:
            result = await handle_command(
                prompt,
                agent,
                TokenTracker(),
                session_state,
                assistant_id,
                session_manager=session_manager,
                model_name=model_name,
            )
        captured = capture.get().strip() or "Command completed."
        if result == "exit":
            return True, captured, None, True
        if isinstance(result, str) and result:
            return True, captured, result, False
        return True, captured, None, False
    except Exception as exc:  # noqa: BLE001 — a broken handler is a command error
        return False, f"{type(exc).__name__}: {exc}", None, False


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
    budget_usd: float | None = getattr(session_state, "headless_max_budget_usd", None)

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

    if prompt == "/context recovery" or prompt.startswith("/context recovery "):
        from novacode_cli.context.archives import handle_recovery_command

        started = time.monotonic()
        code = EXIT_OK
        try:
            result, restored = await handle_recovery_command(
                prompt[len("/context recovery") :].strip(),
                agent=agent,
                thread=session_state.thread_id,
            )
            if restored:
                session_state.thread_id = restored
        except (OSError, ValueError) as error:
            result = str(error)
            code = EXIT_ERROR
        out.result(
            subtype="success" if code == EXIT_OK else "error",
            is_error=code != EXIT_OK,
            result_text=result,
            num_turns=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            usage={},
            exit_code=code,
        )
        return code

    # A slash command runs through the shared registry without an agent turn.
    # A handler that returns a kickoff prompt (e.g. /goal <objective>) is
    # followed by a real agent turn so the caller still gets an answer.
    command_origin: str | None = None
    if prompt.lstrip().startswith("/") and not prompt.startswith("/context recovery"):
        ok, captured, kickoff, wants_exit = await _dispatch_command(
            prompt, agent, session_state, assistant_id, model_name, session_manager
        )
        command_origin = prompt
        code = EXIT_OK if ok else EXIT_ERROR
        if wants_exit or kickoff is None:
            out.result(
                subtype="command",
                is_error=not ok,
                result_text=captured,
                num_turns=0,
                duration_ms=0,
                usage={},
                exit_code=code,
                command=prompt,
            )
            session_state.headless_result_emitted = True
            session_state.headless_exit_code = code
            return code
        # Kickoff: surface the command output as a stream event, then run the
        # kickoff through the agent loop so the caller receives an answer.
        out.handle_event(
            ev.ContextMessage(message=f"[command {prompt}]\n{captured}", event_type="command")
        )
        prompt = kickoff

    # --output-schema: the final answer must validate against the schema, so
    # the model is told up front and the answer is checked at the end.
    schema, schema_error = _load_output_schema(session_state)
    if schema_error:
        out.result(
            subtype="command",
            is_error=True,
            result_text=schema_error,
            num_turns=0,
            duration_ms=0,
            usage={},
            exit_code=EXIT_ERROR,
            command=prompt,
        )
        session_state.headless_result_emitted = True
        session_state.headless_exit_code = EXIT_ERROR
        return EXIT_ERROR
    if schema is not None:
        prompt = _prompt_with_schema(prompt, schema)

    # Image attachments (--image): encoded once here, tracker-bound for the run.
    image_paths = getattr(session_state, "headless_images", None) or []
    image_tracker = None
    if image_paths:
        from novacode_cli.image_utils import ImageData
        from novacode_cli.remote.images import attach_images

        image_tracker = attach_images(None, [ImageData(**image) for image in image_paths])

    started = time.monotonic()
    num_turns = 0
    in_tool_round = False
    main_tool_calls = set()
    tool_call_count = 0
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
    propagate_cancel = False

    # Artifacts persist per session (headless -c/--resume reuses the id).
    try:
        from novacode_cli.artifacts.registry import bind_session

        bind_session(
            getattr(session_state, "session_id", "") or "",
            getattr(session_manager, "sessions_dir", None),
        )
    except Exception:  # noqa: BLE001 — never fail a run on artifact restore
        pass

    # Stream-json init carries the MCP server snapshot so a CI gate can fail
    # when a configured server did not load (Claude-Code-style mcp_server_errors).
    init_tools: list[str] | None = None
    init_mcp_servers: list[dict] | None = None
    init_mcp_errors: list[dict] | None = None
    try:
        from novacode_cli.mcp import get_shared_mcp_middleware

        middleware = get_shared_mcp_middleware()
        middleware.prepare_cached_tools()
        discovery = middleware.start_background_discovery()
        if middleware.server_status:
            # Give configured servers a bounded window to connect so the init
            # snapshot is truthful; discovery continues in the background.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(discovery), timeout=10)
        if middleware.server_status:
            init_mcp_servers = [
                {"name": name, "status": status}
                for name, status in middleware.server_status.items()
            ]
        if middleware.connection_errors:
            init_mcp_errors = [
                {"name": name, "error": error}
                for name, error in middleware.connection_errors.items()
            ]
        init_tools = sorted({tool.name for tool in middleware.tools})
    except Exception:  # noqa: BLE001 — init metadata is best-effort
        pass

    source = iterate_agent_events(
        prompt,
        agent,
        assistant_id,
        session_state,
        backend=backend,
        seen_message_ids=set(),
        image_tracker=image_tracker,
    )

    async def consume(events_source) -> None:
        nonlocal num_turns, in_tool_round, is_error, subtype, exit_code, completed, needs_human
        nonlocal tool_call_count
        out.init(
            tools=init_tools,
            mcp_servers=init_mcp_servers,
            mcp_server_errors=init_mcp_errors,
        )
        async for event in events_source:
            # --- max-turns accounting (a turn = one model step) ---------
            if isinstance(event, ev.ToolCall) and event.is_main_agent:
                tool_call_count += 1
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
            elif isinstance(event, ev.ContextMessage) and event.event_type == "nova_prime_receipt":
                # A host delivery record is output, not another model step.
                text_parts.append(event.message)
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
                if budget_usd is not None:
                    from novacode_cli.tracking.usage_tree import estimate_cost

                    estimated = estimate_cost(
                        usage["input_tokens"], usage["output_tokens"], model_name or ""
                    )
                    if estimated is not None and estimated > budget_usd:
                        is_error = True
                        subtype = "error_budget_exceeded"
                        exit_code = EXIT_ERROR
                        if not text_parts:
                            text_parts.append(
                                f"Budget exceeded: estimated ${estimated:.2f}"
                                f" exceeds --max-budget-usd ${budget_usd:.2f}"
                            )
                        break
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
                outer_budget = getattr(session_state, "headless_bootstrap_budget", None)
                if (
                    budget.expired()
                    or (outer_budget and outer_budget.expired())
                    or (deadline is not None and time.monotonic() >= deadline)
                ):
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
            await consume(source)
            # Wait-at-exit: a turn that started background tasks (a dev server,
            # a watch) waits for them (bounded) and takes one final turn with
            # the outcome — Claude-Code-style, so pipelines see the result.
            if completed and not is_error:
                outcomes = await _wait_for_background_jobs()
                if outcomes:
                    out.handle_event(
                        ev.ContextMessage(
                            message=f"[background tasks finished]\n{outcomes}",
                            event_type="background",
                        )
                    )
                    follow_up = (
                        "Background tasks you started have finished:\n"
                        f"{outcomes}\n\n"
                        "Summarize the outcome of the background work for the caller."
                    )
                    await consume(
                        iterate_agent_events(
                            follow_up,
                            agent,
                            assistant_id,
                            session_state,
                            backend=backend,
                            seen_message_ids=set(),
                        )
                    )
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
        propagate_cancel = getattr(session_state, "headless_bootstrap_active", False)
        is_error = True
        outer_budget = getattr(session_state, "headless_bootstrap_budget", None)
        if (
            budget.expired()
            or (outer_budget and outer_budget.expired())
            or (deadline is not None and time.monotonic() >= deadline)
        ):
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
        from novacode_cli.sessions.scratchpad import cleanup as cleanup_scratchpad

        cleanup_scratchpad(getattr(session_state, "session_id", None))

    duration_ms = int((time.monotonic() - started) * 1000)
    result_text = "\n".join(p for p in text_parts if p).strip()

    # --output-schema: validate the final answer; structured_output appears
    # only when it actually validates (Claude-Code behavior).
    structured: Any | None = None
    if schema is not None and result_text and not is_error:
        valid, structured = _structured_output(result_text, schema)
        if not valid:
            _diagnostic(
                "--output-schema: the final answer did not validate; no structured_output emitted"
            )
            structured = None

    # Client-side cost estimate for the observed model (None when unknown).
    total_cost: float | None = None
    if usage.get("input_tokens") or usage.get("output_tokens"):
        try:
            from novacode_cli.tracking.usage_tree import estimate_cost

            total_cost = estimate_cost(
                usage["input_tokens"], usage["output_tokens"], model_name or ""
            )
        except Exception:  # noqa: BLE001
            total_cost = None

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
            command=command_origin,
            tool_call_count=tool_call_count,
            structured_output=structured,
            total_cost_usd=total_cost,
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

    outer_budget = getattr(session_state, "headless_bootstrap_budget", None)
    if propagate_cancel or (outer_budget and outer_budget.expired()):
        # The outer deadline must stop initialization/cleanup too. Swallowing
        # its cancellation here lets the caller continue after the deadline.
        raise asyncio.CancelledError
    return exit_code


async def _wait_for_background_jobs() -> str | None:
    """Wait (bounded) for background tasks the turn started; summarize outcomes.

    Returns None when nothing is running or the wait is disabled
    (``NOVA_HEADLESS_BG_WAIT_SECONDS=0``).
    """
    import os as _os

    try:
        from novacode_cli.shell.jobs import get_registry

        registry = get_registry()
    except Exception:  # noqa: BLE001 — no registry, nothing to wait for
        return None
    try:
        wait_seconds = float(
            _os.environ.get("NOVA_HEADLESS_BG_WAIT_SECONDS", BG_WAIT_DEFAULT_SECONDS)
        )
    except ValueError:
        wait_seconds = BG_WAIT_DEFAULT_SECONDS
    if wait_seconds <= 0:
        return None
    try:
        running = [job for job in registry.list_jobs() if job.status == "running"]
    except Exception:  # noqa: BLE001
        return None
    if not running:
        return None
    _diagnostic(f"Waiting for {len(running)} background task(s) to finish...")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        try:
            if all(job.status != "running" for job in registry.list_jobs()):
                break
        except Exception:  # noqa: BLE001
            return None
    lines = []
    try:
        for job in registry.list_jobs():
            if job.status == "running":
                lines.append(f"- {job.task_id}: still running ({job.command[:80]})")
            else:
                lines.append(f"- {job.task_id} ({job.command[:80]}): exited {job.exit_code}")
    except Exception:  # noqa: BLE001
        return None
    return "\n".join(lines) or None


async def run_bootstrap(awaitable, session_state):
    """Include initialization in the deadline and emit structured startup errors."""
    started = time.monotonic()
    timeout = getattr(session_state, "headless_timeout", None)
    if timeout is not None:
        session_state.headless_deadline = started + timeout
    budget = asyncio.timeout(timeout)
    session_state.headless_bootstrap_active = True
    session_state.headless_bootstrap_budget = budget
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
    finally:
        session_state.headless_bootstrap_active = False
        session_state.headless_bootstrap_budget = None
        from novacode_cli.sessions.scratchpad import cleanup as cleanup_scratchpad

        cleanup_scratchpad(getattr(session_state, "session_id", None))
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
        from novacode_cli.commands.side_commands import goal_state_payload
        from novacode_cli.prime import prime_payload
        from novacode_cli.remote.session import remote_connections_payload

        await asyncio.to_thread(
            session_manager.save_session,
            session_id=session_state.session_id,
            thread_id=session_state.thread_id,
            messages=messages,
            assistant_id=assistant_id,
            todos=todos,
            model_name=model_name,
            model_provider=getattr(session_state, "session_model_provider", None)
            or get_current_provider(),
            project_root=settings.project_root or Path.cwd(),
            task_status="failed" if is_error else "completed",
            goal=goal_state_payload(session_state),
            prime=prime_payload(session_state),
            remote_connections=remote_connections_payload(session_state),
        )
    except Exception as exc:  # noqa: BLE001 — never fail the run on save
        _diagnostic(f"Headless session save skipped: {exc}")

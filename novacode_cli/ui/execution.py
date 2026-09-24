"""Task execution — shared utilities for agent event streaming.

This module contains the ``_capture_fallback_usage`` helper used by the
agent loop to capture token usage from the persisted final AIMessage
when the streaming path doesn't surface usage metadata.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from rich.markdown import Markdown
from rich.text import Text

from novacode_cli import ui_events as ev
from novacode_cli.config.config import COLORS, console, get_agent_color
from novacode_cli.core.agent_loop import default_interrupt_response
from novacode_cli.core.autonomous_loop import run_with_goal
from novacode_cli.file_ops import get_session_file_op_tracker
from novacode_cli.input_utils import ImageTracker
from novacode_cli.ui import status_phrases
from novacode_cli.core.input_preparation import (
    build_agent_config,
    get_agent_display_name,
)
from novacode_cli.ui.interrupt_handlers import (
    handle_plan_approval_interrupt,
    handle_question_interrupt,
)
from novacode_cli.ui.ui_elements import (
    TokenTracker,
    render_file_operation,
    render_todo_list,
    render_tool_panel,
)


async def _react_to_context_pressure(agent, session_state, token_tracker, breakdown) -> None:
    """Warn about — and where appropriate auto-compact — high context usage.

    The Rich console REPL had no equivalent of the TUI's context check, so a
    long session neither warned the user nor compacted and ran straight into the
    provider's limit. The decision itself is shared
    (:func:`novacode_cli.context.assess_pressure`) so both surfaces agree.

    Never raises: a warning is not worth failing a completed turn.
    """
    try:
        from novacode_cli.context import (
            PressureAction,
            assess_pressure,
            post_compaction_still_critical,
        )

        pct = getattr(breakdown, "usage_percentage", 0.0)
        decision = assess_pressure(
            pct,
            compacted_last_turn=bool(getattr(session_state, "_compacted_last_turn", False)),
            auto_compact_enabled=bool(getattr(session_state, "_auto_compact", True)),
        )
        if decision.disable_auto_compact:
            session_state._auto_compact = False
        # A COMPACT decision is only a plan: the flag flips to True below, on a
        # compaction that actually succeeded. Setting it here would trip the loop
        # guard next turn after a compaction that never ran.
        session_state._compacted_last_turn = (
            False if decision.action is PressureAction.COMPACT else decision.compacted_last_turn
        )

        if decision.action is PressureAction.COMPACT:
            # compact_conversation needs a real BaseChatModel (it calls
            # get_num_tokens_from_messages and drives the summary LLM), so a bare
            # model *name* is not usable. Prefer the session's live model object;
            # if none is reachable, warn instead of attempting a broken call.
            from langchain_core.language_models import BaseChatModel

            model = getattr(session_state, "model", None) or getattr(agent, "model", None)
            if not isinstance(model, BaseChatModel):
                console.print(
                    f"[bold #f7768e]⚠ {decision.reason} "
                    "Auto-compact is unavailable on this surface — run /compact.[/]"
                )
                return

            console.print(f"[bold #f7768e]⚠ {decision.reason}[/]")
            from novacode_cli.compaction import compact_conversation

            result = await compact_conversation(
                agent,
                model,
                getattr(session_state, "session_id", "") or "",
                agent_dir=getattr(session_state, "agent_dir", None),
            )
            if result.success:
                token_tracker.reset()
                console.print(
                    f"[dim]Compacted: {result.tokens_saved:,} tokens freed "
                    f"({result.messages_before} → {result.messages_after} messages).[/]"
                )
                session_state._compacted_last_turn = True
                after = post_compaction_still_critical(
                    getattr(token_tracker.get_breakdown(), "usage_percentage", 0.0),
                    auto_compact_enabled=bool(getattr(session_state, "_auto_compact", True)),
                )
                if after.disable_auto_compact:
                    session_state._auto_compact = False
                    console.print(f"[bold #f7768e]⚠ {after.reason}[/]")
            else:
                console.print(f"[#e0af68]Could not compact: {result.error}[/]")
        elif decision.action is PressureAction.WARN:
            console.print(f"[#e0af68]⚠ {decision.reason}[/]")
    except Exception:  # noqa: BLE001 — a notice must never break the turn
        return


def _bound_tools(agent: Any) -> list[Any] | None:
    """Best-effort extraction of the agent's bound tool definitions.

    Tool schemas ship on every request and are a permanent slice of the
    baseline, but they live on the compiled graph, not in the message list.

    Prefers the live ``ToolNode.tools_by_name`` mapping (a documented attribute)
    over reaching into ``PregelNode.bound``, whose shape is an implementation
    detail. Purely cosmetic accounting: any failure yields ``None``, which just
    leaves ``tool_definitions_tokens`` at 0.
    """
    try:
        from langgraph.prebuilt import ToolNode

        for node in (getattr(agent, "nodes", None) or {}).values():
            bound = getattr(node, "bound", None)
            if isinstance(bound, ToolNode):
                mapping = getattr(bound, "tools_by_name", None)
                if mapping:
                    return list(mapping.values())
    except Exception:  # noqa: BLE001 — accounting only, never fatal
        return None
    return None


async def _capture_fallback_usage(agent, config, token_tracker: TokenTracker) -> None:
    """Capture token usage from the persisted final AIMessage.

    Some providers (notably Ollama) don't surface ``usage_metadata`` on the
    final streamed chunk, so the primary capture path stays at zero. This
    helper reads the aggregated AIMessage from the persisted graph state
    instead. Best-effort — never raises.
    """
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return
        # Walk backwards to find the most recent message with usage_metadata.
        for m in reversed(messages):
            usage = getattr(m, "usage_metadata", None)
            if usage and isinstance(usage, dict):
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                if inp > 0 or out > 0:
                    token_tracker.current_context = inp
                    token_tracker.last_output = out
                    token_tracker.has_api_data = True
                return
    except Exception:
        return


async def execute_task(  # type: ignore

    user_input: str,

    agent,

    assistant_id: str | None,

    session_state,

    token_tracker: TokenTracker | None = None,

    backend=None,

    is_subagent: bool = False,

    image_tracker: ImageTracker | None = None,

    seen_message_ids: set[str] | None = None,

    *,

    skip_file_mentions: bool = False,

    verify: bool = False,

) -> None:

    """Execute any task by passing it directly to the AI agent.



    Wraps :func:`~novacode_cli.core.agent_loop.iterate_agent_events`, rendering

    each :mod:`novacode_cli.ui_events` event to the rich console.  Handles

    keyboard interrupt and token tracking around the event loop.



    When ``verify`` (or ``session_state.verify_enabled``) is set, the run is

    routed through :func:`~novacode_cli.core.verification_loop.run_with_verification`,

    which grades each turn against a rubric and retries with feedback on failure.

    """

    from novacode_cli.vixie.server import (

        set_idle as vixie_set_idle,

    )

    from novacode_cli.vixie.server import (

        set_thinking as vixie_set_thinking,

    )

    from novacode_cli.vixie.server import (

        set_working as vixie_set_working,

    )



    # NB: input content (file mentions + images) is prepared inside

    # iterate_agent_events; preparing it here too would read mentioned files

    # off disk a second time.

    thread_id = str(uuid.uuid4()) if is_subagent else session_state.thread_id

    config = build_agent_config(thread_id, assistant_id)



    agent_display_name = get_agent_display_name(assistant_id)

    agent_colors = (

        get_agent_color(assistant_id)

        if assistant_id and assistant_id != "nova-agent"

        else (COLORS["success"] if assistant_id == "nova-agent" else COLORS["agent"])

    )



    if token_tracker:

        token_tracker.increment_user_messages()



    has_responded = False

    captured_input_tokens = 0

    captured_output_tokens = 0

    captured_cache_read_tokens = 0

    captured_cache_creation_tokens = 0

    current_todos = None

    _prev_auto_approve: bool | None = None

    _last_known_state: object = None



    try:

        import logging as _vixie_logging



        _vixie_logging.getLogger("vixie").info("Setting Vixie state to THINKING")

        await vixie_set_thinking()

        _vixie_logging.getLogger("vixie").info("Vixie state set to THINKING complete")

    except Exception as e:

        import logging as _logging



        _logging.getLogger("vixie").warning(f"Failed to set Vixie state: {e}")



    status = console.status(

        f"[bold {agent_colors}]{status_phrases.status_line('thinking', agent_display_name)}",

        spinner="dots",

    )

    status.start()

    spinner_active = True



    file_op_tracker = get_session_file_op_tracker(assistant_id=assistant_id, backend=backend)



    displayed_tool_ids: set[str] = set()

    _tool_preview_buffer: list[Text] = []



    import os as _os



    _DEBUG_ENABLED = _os.environ.get("Nova_DEBUG", "").lower() in ("1", "true", "yes")



    def _dbg(tag: str, msg: str) -> None:

        pass



    if _DEBUG_ENABLED:

        import datetime as _dt



        _DBG_PATH = Path("_Nova_debug.log")



        def _dbg(tag: str, msg: str) -> None:  # type: ignore

            with open(_DBG_PATH, "a", encoding="utf-8") as f:

                f.write(f"[{_dt.datetime.now():%H:%M:%S.%f}] [{tag}] {msg}\n")



    _dbg(

        "START",

        f"=== new execute_task ===  plan_mode={getattr(session_state, 'plan_mode_enabled', '?')}",

    )



    subagent_remaining = 0

    subagent_done = 0



    def flush_tool_previews() -> None:

        """Print all buffered tool-result preview lines in a single stop/start cycle."""

        nonlocal spinner_active

        if not _tool_preview_buffer:

            return

        if spinner_active:

            status.stop()

            spinner_active = False

        for _detail in _tool_preview_buffer:

            console.print(_detail)

        _tool_preview_buffer.clear()

        if not spinner_active:

            status.start()

            spinner_active = True



    # Opt-in inline verification loop (Enhancement 1). Falls back to the plain

    # canonical generator when off, so existing callers are unaffected.

    _verify = verify or bool(getattr(session_state, "verify_enabled", False))

    if _verify and not is_subagent:

        from novacode_cli.core.verification_loop import run_with_verification

        from novacode_cli.hermes.verifier import InlineVerifier

        from novacode_cli.memory.store import get_durable_store



        _event_source = run_with_verification(

            user_input,

            agent,

            assistant_id,

            session_state,

            backend=backend,

            image_tracker=image_tracker,

            seen_message_ids=seen_message_ids,

            skip_file_mentions=skip_file_mentions,

            verifier=InlineVerifier(get_durable_store(), enabled=True),

        )

    else:

        _event_source = run_with_goal(

            user_input,

            agent,

            assistant_id,

            session_state,

            backend=backend,

            image_tracker=image_tracker,

            seen_message_ids=seen_message_ids,

            skip_file_mentions=skip_file_mentions,

        )



    try:

        async for event in _event_source:

            if isinstance(event, ev.StatusUpdate):

                if spinner_active and event.message:

                    status.update(f"[bold {COLORS['thinking']}]{event.message}")



            elif isinstance(event, ev.TextDelta):

                pass  # Rich batching: text accumulated in core loop, rendered via AssistantMessage



            elif isinstance(event, ev.TextDiscard):

                pass  # No live preview in rich console



            elif isinstance(event, ev.ReasoningDelta):

                pass  # Silently skip in rich console



            elif isinstance(event, ev.AssistantMessage):

                if spinner_active:

                    status.stop()

                    spinner_active = False

                if not has_responded:

                    console.print()

                    console.print("●", style=agent_colors, markup=False, end=" ")

                    has_responded = True

                _dbg("FLUSH-PRINT", f"text[:120]={event.text[:120]!r}")

                md = Markdown(event.text.rstrip())

                console.print(agent_display_name, style=agent_colors)

                console.print(md, justify="full")

                try:

                    from novacode_cli.hooks import (

                        HookEvent,

                        dispatch_hook_fire_and_forget,

                    )



                    dispatch_hook_fire_and_forget(

                        HookEvent.AGENT_MESSAGE,

                        {

                            "session_id": getattr(session_state, "session_id", ""),

                            "thread_id": getattr(session_state, "thread_id", ""),

                            "message": event.text[:500],

                        },

                    )

                except Exception:

                    pass



            elif isinstance(event, ev.ToolCall):

                flush_tool_previews()

                if spinner_active:

                    status.stop()

                    spinner_active = False



                if has_responded:

                    console.print()



                render_tool_panel(event.name, event.display_str, event.icon)



                try:

                    from novacode_cli.hooks import (

                        HookEvent,

                        dispatch_hook_fire_and_forget,

                    )



                    dispatch_hook_fire_and_forget(

                        HookEvent.TOOL_CALL,

                        {

                            "tool": event.name,

                            "args": str(event.args)[:500] if event.args else "",

                            "session_id": getattr(session_state, "session_id", ""),

                        },

                    )

                except Exception:

                    pass



                _notify = getattr(session_state, "_remote_tool_notify", None)

                if _notify is not None:

                    try:

                        _notify(event.name, event.display_str)

                    except Exception:

                        pass



                try:

                    await vixie_set_working()

                except Exception:

                    pass



            elif isinstance(event, ev.ToolResult):

                sty = "red" if event.is_error else f"dim {COLORS['tool']}"

                detail = Text()

                detail.append("  ⎿  ", style=sty)

                detail.append(event.preview, style=sty)

                _tool_preview_buffer.append(detail)



                try:

                    from novacode_cli.hooks import (

                        HookEvent,

                        dispatch_hook_fire_and_forget,

                    )



                    dispatch_hook_fire_and_forget(

                        HookEvent.TOOL_RESULT,

                        {

                            "tool": "",

                            "status": "success" if not event.is_error else "error",

                            "preview": event.preview[:300],

                            "session_id": getattr(session_state, "session_id", ""),

                        },

                    )

                except Exception:

                    pass



                if spinner_active:

                    if subagent_done + subagent_remaining > 0:

                        total = subagent_done + subagent_remaining

                        if subagent_remaining > 0:

                            status.update(

                                f"[bold {COLORS['thinking']}]"

                                f"{subagent_done}/{total} subagents done, "

                                f"waiting for {subagent_remaining} more..."

                            )

                        else:

                            label = "subagent" if subagent_done == 1 else "subagents"

                            status.update(

                                f"[bold {COLORS['thinking']}]"

                                f"All {subagent_done} {label} done, stitching it together..."

                            )

                    else:

                        status.update(

                            f"[bold {COLORS['thinking']}]"
                            f"{status_phrases.status_line('thinking', agent_display_name)}"

                        )



            elif isinstance(event, ev.FileOp):

                flush_tool_previews()

                if spinner_active:

                    status.stop()

                    spinner_active = False

                console.print()

                render_file_operation(event.record)

                console.print()

                if not spinner_active:

                    status.start()

                    spinner_active = True



            elif isinstance(event, ev.TodoUpdate):

                if spinner_active:

                    status.stop()

                    spinner_active = False

                current_todos = event.todos

                console.print()

                render_todo_list(event.todos, agent_name=event.agent_name)

                console.print()

                if not spinner_active:

                    status.start()

                    spinner_active = True

                # Mirror the plan into the remote status line (legacy bridge path).

                _todo_notify = getattr(session_state, "_remote_todo_notify", None)

                if _todo_notify is not None:

                    try:

                        _todo_notify(event.todos)

                    except Exception:  # noqa: BLE001

                        pass



            elif isinstance(event, ev.SubagentActivity):

                if event.kind == "dispatched":

                    subagent_remaining += 1

                    if subagent_remaining > 1:

                        status.update(

                            f"[bold {COLORS['thinking']}]"

                            f"{subagent_remaining} agents out on errands..."

                        )

                    else:

                        sub_color = event.color or COLORS["thinking"]

                        status.update(
                            f"[bold {sub_color}]"
                            f"{status_phrases.status_line('subagent', event.subagent_type)}"
                        )

                    if not spinner_active:

                        status.start()

                        spinner_active = True

                elif event.kind == "completed":

                    subagent_remaining -= 1

                    subagent_done += 1

                    if spinner_active:

                        rem = subagent_remaining

                        done = subagent_done

                        total = done + rem

                        if rem > 0:

                            status.update(

                                f"[bold {COLORS['thinking']}]"

                                f"{done}/{total} subagents done, "

                                f"waiting for {rem} more..."

                            )

                        else:

                            label = "subagent" if done == 1 else "subagents"

                            status.update(

                                f"[bold {COLORS['thinking']}]"

                                f"All {done} {label} done, stitching it together..."

                            )



            elif isinstance(event, ev.InterruptRequest):

                # Resolve in a finally so a handler that raises before

                # set_result fails closed (reject) instead of leaving the agent

                # loop awaiting the future forever.

                try:

                    if event.kind == "tool":
                        # New contract: agent_loop already applied the policy gate
                        # (evaluate_tool_actions / auto_approve / plan mode) and
                        # only surfaces a genuine "ask". The legacy console approval
                        # UI was removed with the legacy REPL, so this renderer
                        # (used by autonomous command handlers) blocks plan-mode
                        # writes and otherwise approves.
                        from novacode_cli.ui.hitl_approval import (
                            check_plan_mode_blocked,
                        )

                        req = event.payload
                        blocked, rejection = check_plan_mode_blocked(
                            req,
                            getattr(session_state, "plan_mode_enabled", False),
                        )
                        if blocked and rejection:
                            event.future.set_result(
                                {
                                    "decisions": rejection["decisions"],
                                    "any_rejected": True,
                                }
                            )
                        else:
                            _ars = (req or {}).get("action_requests", [])
                            event.future.set_result(
                                {
                                    "decisions": [
                                        {"type": "approve"} for _ in _ars
                                    ],
                                    "any_rejected": False,
                                }
                            )

                    elif event.kind == "question":

                        _dbg(

                            "HITL-QUESTION",

                            f"auto_approve={session_state.auto_approve}",

                        )

                        response, spinner_active = await handle_question_interrupt(

                            question_request=event.payload,

                            auto_approve=session_state.auto_approve,

                            spinner_active=spinner_active,

                            status=status,

                        )

                        event.future.set_result(response)

                        if not spinner_active:

                            status.start()

                            spinner_active = True



                    elif event.kind == "plan":

                        _dbg(

                            "HITL-PLAN",

                            f"plan_mode={getattr(session_state, 'plan_mode_enabled', '?')}",

                        )

                        (

                            response,

                            occurred,

                            spinner_active,

                            cmd_state_update,

                        ) = await handle_plan_approval_interrupt(

                            current_todos=current_todos,

                            session_state=session_state,

                            spinner_active=spinner_active,

                            status=status,

                            dbg_func=_dbg,

                            interrupt_payload=event.payload,

                        )

                        event.future.set_result(

                            {

                                "response": response,

                                "state_update": cmd_state_update,

                            }

                        )

                finally:

                    if not event.future.done():

                        event.future.set_result(default_interrupt_response(event.kind))



            elif isinstance(event, ev.ErrorOutput):

                if spinner_active:

                    status.stop()

                    spinner_active = False

                console.print(f"[yellow]{event.text}[/yellow]")

                if not spinner_active:

                    status.start()

                    spinner_active = True



            elif isinstance(event, ev.Error):

                if spinner_active:

                    status.stop()

                    spinner_active = False

                # Provider failures (usage/rate limit, auth, connectivity) are

                # pre-formatted into a clean notice upstream and flagged; show

                # them as a calm warning rather than a raw red error.

                notice = event.message if event.is_provider_notice else None

                if notice is None and event.exception is not None:

                    from novacode_cli.errors import friendly_model_error



                    notice = friendly_model_error(event.exception)

                if notice:

                    console.print(f"[yellow]{notice}[/yellow]")

                else:

                    console.print(f"[red]{event.message}[/red]")

                return



            elif isinstance(event, ev.Cancelled):

                if spinner_active:

                    status.stop()

                console.print("\n[yellow]Interrupted by user[/yellow]")

                try:

                    await asyncio.shield(vixie_set_idle())

                except Exception:

                    pass

                return



            elif isinstance(event, ev.CompactionNotice):

                console.print()

                console.print("[dim]⟳ Context compacted — old messages replaced with summary[/dim]")



            elif isinstance(event, ev.ContextMessage):

                flush_tool_previews()

                if spinner_active:

                    status.stop()

                    spinner_active = False

                console.print()

                console.print(

                    f"{event.icon}  {event.message}",

                    style=event.color,

                )

                if not spinner_active:

                    status.start()

                    spinner_active = True



            elif isinstance(event, ev.UsageUpdate):

                captured_input_tokens = max(captured_input_tokens, event.input_tokens)

                captured_output_tokens = max(captured_output_tokens, event.output_tokens)

                captured_cache_read_tokens = max(

                    captured_cache_read_tokens, event.cache_read_tokens

                )

                captured_cache_creation_tokens = max(

                    captured_cache_creation_tokens, event.cache_creation_tokens

                )



            elif isinstance(event, ev.Done):

                has_responded = event.had_response



    except KeyboardInterrupt:

        if spinner_active:

            status.stop()

        console.print("\n[yellow]Interrupted[/yellow]")



        try:

            await asyncio.shield(vixie_set_idle())

        except Exception:

            pass



        # Let the graph know about the keyboard interrupt

        try:

            await asyncio.wait_for(

                asyncio.shield(

                    agent.aupdate_state(

                        config=config,

                        values={

                            "messages": [

                                HumanMessage(

                                    content="[User interrupted the previous request with Ctrl+C]"

                                )

                            ]

                        },

                        as_node="model",

                    )

                ),

                timeout=3.0,

            )

        except Exception:

            pass



        return



    except asyncio.CancelledError:

        if spinner_active:

            status.stop()

        console.print("\n[yellow]Interrupted by user[/yellow]")

        try:

            await asyncio.shield(vixie_set_idle())

        except Exception:

            pass

        return



    # ------------------------------------------------------------------

    # Finalization

    # ------------------------------------------------------------------

    if spinner_active:

        status.stop()



    if current_todos:

        session_state.todos = current_todos



    if _prev_auto_approve is not None:

        session_state.auto_approve = _prev_auto_approve



    if has_responded:

        console.print()

        if token_tracker and (captured_input_tokens or captured_output_tokens):

            token_tracker.add(

                captured_input_tokens,

                captured_output_tokens,

                cache_read_tokens=captured_cache_read_tokens,

                cache_creation_tokens=captured_cache_creation_tokens,

            )

        elif token_tracker and not captured_input_tokens:

            # Stream mode didn't surface usage (e.g. Ollama) — fall back to the

            # usage_metadata on the persisted final AIMessage.

            await _capture_fallback_usage(agent, config, token_tracker)

        if token_tracker:

            token_tracker.increment_assistant_messages()

            if displayed_tool_ids:

                token_tracker.increment_tool_calls(len(displayed_tool_ids))



            try:

                from novacode_cli.context import ContextManager



                _bd_state = _last_known_state

                if _bd_state is None:

                    _bd_state = await agent.aget_state(config)

                    _last_known_state = _bd_state

                _bd_msgs = _bd_state.values.get("messages", [])

                if _bd_msgs and token_tracker.model_name:

                    _bd_tools = getattr(session_state, "_tools", None) or _bound_tools(agent)
                    breakdown = ContextManager(token_tracker.model_name).breakdown(
                        _bd_msgs, tools=_bd_tools
                    )

                    token_tracker.set_breakdown(breakdown)

                    # Same policy the TUI applies (context/pressure.py) — the
                    # REPL previously recorded the breakdown and then did
                    # NOTHING, so a long session sailed into the provider's
                    # limit with no warning and no compaction.
                    await _react_to_context_pressure(
                        agent, session_state, token_tracker, breakdown
                    )

            except Exception:

                pass



        try:

            await vixie_set_idle()

        except Exception:

            pass


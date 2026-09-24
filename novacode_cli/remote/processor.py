"""Remote message processor — reads from the shared queue and runs the agent.

This module provides the long-running background task that dequeues
``RemoteMessage`` objects produced by Discord/Telegram bridges and
feeds them through ``execute_task()`` just like a local prompt.

The processor serialises access with an ``asyncio.Lock`` so remote and
local messages do not interleave mid-stream.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_DEBUG_LOG = os.path.expanduser("~/.nova/remote_debug.log")


async def _debug_log(line: str) -> None:
    """Write a line to the debug log without blocking the event loop."""
    line = str(line) + "\n"
    await asyncio.to_thread(_write_debug, line)


def _write_debug(line: str) -> None:
    """Synchronous helper that does the actual file write."""
    os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
    with open(_DEBUG_LOG, "a") as f:
        f.write(line)


def _condense_response(raw: str) -> str:
    """Strip verbose/internal scaffolding from the agent's response for Discord.

    The agent's raw output often contains internal meta-commentary that makes
    sense in a terminal but clutters a chat message. This function strips:

    * \"I'll...\" / \"Let me...\" / \"Now I'll...\" prefatory lines
    * \"I used X tool to...\" self-narration
    * Duplicate/echo of the user's own request
    * Excessive blank lines

    Args:
        raw: The raw concatenated AIMessage content.

    Returns:
        Condensed text suitable for a chat platform.
    """
    text = raw.strip()
    if not text:
        return text

    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()

        # Skip lines that are just the agent narrating what it's about to do
        if re.match(
            r"^(I'?ll|Let me|Now I'?ll|First,? I'?ll|I should|I need to|"
            r"I want to|I will now|Going to|About to)",
            stripped,
            re.IGNORECASE,
        ):
            continue

        # Skip lines that are just tool self-narration
        if re.match(
            r"^(I used|I ran|I executed|I called|Using the|With the|"
            r"The (tool|command|output|result) (show|indicate|return|gave|was))",
            stripped,
            re.IGNORECASE,
        ):
            continue

        # Skip lines that literally echo "The user asked: / said:"
        if re.match(r"^(The user|You asked|You said|User asked)", stripped, re.IGNORECASE):
            continue

        cleaned.append(stripped)

    # Rejoin, collapse runs of blank lines to at most one
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


async def remote_message_processor(
    queue: asyncio.Queue,
    agent: Any,
    assistant_id: str,
    session_state: Any,
    console: Any,
    token_tracker: Any,
    backend: Any,
    image_tracker: Any,
    seen_message_ids: Any,
    execute_fn: Any | None = None,
) -> None:
    """Process messages from the remote message queue."""
    from novacode_cli.config.config import COLORS

    lock = getattr(session_state, "_remote_message_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        session_state._remote_message_lock = lock

    logger.info("Remote message processor started, queue=%s", id(queue))
    await _debug_log(f"PROCESSOR STARTED queue={id(queue)}")

    console.print("\n  [dim]\U0001f517 Remote message processor ready[/]\n")

    active_tasks: set[asyncio.Task] = set()

    async def _handle_side_command(remote_msg: Any, *, allow_kickoff: bool) -> str:
        """Handle the /goal and /btw side commands for the remote bridge.

        Shares semantics with the TUI via :mod:`novacode_cli.commands.side_commands`.

        Args:
            remote_msg: The inbound remote message (its ``text`` may be rewritten).
            allow_kickoff: When True (no turn running), a ``/goal <desc>`` may run
                its autonomous kick-off as a turn; when False (busy), the goal is
                only recorded for the next turn.

        Returns:
            ``"done"`` (fully handled — stop), ``"run"`` (``remote_msg.text`` has
            been replaced with a prompt to run as a normal turn), or ``"pass"``
            (not a side command — proceed normally).
        """
        text = remote_msg.text.strip()
        low = text.lower()

        if low == "/goal" or low.startswith("/goal "):
            from novacode_cli.commands.side_commands import handle_goal_command

            args = text[len("/goal"):].strip()
            result = handle_goal_command(session_state, args)
            msg = result.message
            if result.kickoff and not allow_kickoff:
                msg += "\n\n(Recorded — it will guide the task already in progress.)"
            try:
                await remote_msg.reply_fn(msg)
            except Exception:
                pass
            if result.kickoff and allow_kickoff:
                remote_msg.text = result.kickoff
                return "run"
            return "done"

        if low == "/btw" or low.startswith("/btw "):
            from novacode_cli.commands.side_commands import run_btw_question

            question = text[len("/btw"):].strip()

            async def _btw_task() -> None:
                answer = await run_btw_question(question)
                try:
                    await remote_msg.reply_fn(answer)
                except Exception:
                    pass

            # Run concurrently so the side question never blocks the main turn
            # (or the message dequeue loop while a turn is active).
            btw = asyncio.create_task(_btw_task())
            active_tasks.add(btw)
            btw.add_done_callback(active_tasks.discard)
            return "done"

        return "pass"

    async def run_turn(remote_msg: Any) -> None:
        try:
            logger.info(
                "Remote message from %s on %s",
                remote_msg.user_name, remote_msg.platform.value
            )

            console.print()
            console.print(
                f"  [{COLORS.get('primary', 'cyan')}]\U0001f4e1 Remote ({remote_msg.platform.value})[/"
                f" [{COLORS.get('user', 'green')}]{remote_msg.user_name}[/]: "
                f"{remote_msg.text[:120]}"
            )
            console.print()

            try:
                from novacode_cli.hooks import HookEvent, dispatch_hook_fire_and_forget
                dispatch_hook_fire_and_forget(HookEvent.REMOTE_MESSAGE, {
                    "platform": remote_msg.platform.value,
                    "user": remote_msg.user_name,
                    "text": remote_msg.text[:500],
                    "session_id": getattr(session_state, "session_id", ""),
                })
            except Exception:
                pass

            # Side commands (/goal, /btw) — handled before the normal agent turn.
            # "/goal <desc>" rewrites remote_msg.text to its kick-off and falls
            # through to run as a turn; everything else is fully handled here.
            _side = await _handle_side_command(remote_msg, allow_kickoff=True)
            if _side == "done":
                return

            async with lock:
                try:
                    _typing_cm = None
                    typing_task: asyncio.Task | None = None
                    typing_fn = getattr(remote_msg, "typing_fn", None)
                    if typing_fn is not None:
                        if inspect.iscoroutinefunction(typing_fn):
                            async def _typing_loop():
                                try:
                                    while True:
                                        await typing_fn()
                                        await asyncio.sleep(8)
                                except asyncio.CancelledError:
                                    pass
                            typing_task = asyncio.create_task(_typing_loop())
                        else:
                            try:
                                _typing_cm = typing_fn()
                                if _typing_cm is not None and hasattr(_typing_cm, "__aenter__"):
                                    await _typing_cm.__aenter__()
                                else:
                                    _typing_cm = None
                            except Exception:
                                _typing_cm = None

                    from novacode_cli.ui import status_phrases

                    console.print(
                        f"  [{COLORS.get('dim', 'dim')}]\U0001f914 "
                        f"Nova {status_phrases.rotating('thinking')}[/]"
                    )

                    # A compact status line edits in place to show live tool/
                    # subagent activity (condensed counts), SEPARATE from the
                    # answer (sent as a fresh message at the end).
                    edit_fn = getattr(remote_msg, "edit_fn", None)
                    status = None
                    if edit_fn is not None:
                        from novacode_cli.remote.status import RemoteStatusLine

                        status = RemoteStatusLine(edit_fn)
                        status.start()

                    _prev_auto_approve = getattr(session_state, "auto_approve", False)
                    session_state.auto_approve = True

                    _tool_names: list[str] = []

                    def _record_tool(name, info=None, *, is_result=False):  # noqa: ARG001
                        """Hook per tool call — collect names + feed the status line."""
                        if is_result or not name:
                            return
                        _tool_names.append(str(name))
                        if status is not None:
                            status.note(str(name))

                    session_state._remote_tool_notify = _record_tool
                    if status is not None:
                        session_state._remote_todo_notify = status.note_todos

                    try:
                        config = {
                            "configurable": {"thread_id": session_state.thread_id}
                        }
                        pre_state = await agent.aget_state(config)
                        pre_msg_count = len(
                            pre_state.values.get("messages", [])
                        ) if pre_state else 0

                        if execute_fn is not None:
                            await execute_fn(
                                remote_msg.text,
                                agent,
                                assistant_id,
                                session_state,
                                token_tracker,
                                backend=backend,
                                is_subagent=False,
                                image_tracker=image_tracker,
                                seen_message_ids=seen_message_ids,
                                skip_file_mentions=True,
                            )

                        post_state = await agent.aget_state(config)
                        response_text = _extract_response(post_state, pre_msg_count)

                        # Settle the status line, then send the answer as its own
                        # message (the status carries the tool/subagent summary).
                        if status is not None:
                            await status.finalize()
                        condensed = (
                            _condense_response(response_text) if response_text else ""
                        )
                        final_text = condensed or "✅ Task completed (no text response)."
                        if getattr(remote_msg, "user_mention", None):
                            final_text = f"{remote_msg.user_mention}\n{final_text}"
                        try:
                            await remote_msg.reply_fn(final_text)
                        except Exception:
                            pass

                    finally:
                        session_state.auto_approve = _prev_auto_approve
                        session_state._remote_tool_notify = None
                        session_state._remote_todo_notify = None

                        if typing_task is not None:
                            typing_task.cancel()
                            try:
                                await typing_task
                            except asyncio.CancelledError:
                                pass
                        if _typing_cm is not None:
                            try:
                                await _typing_cm.__aexit__(None, None, None)
                            except Exception:
                                pass

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error processing remote message: {e}")
                    try:
                        await remote_msg.reply_fn(
                            f"❌ Error: {str(e)[:200]}"
                        )
                    except Exception:
                        pass
        except Exception as ex:
            logger.error(f"Unhandled exception in remote run_turn: {ex}")
        finally:
            queue.task_done()

    while True:
        try:
            remote_msg = await queue.get()
            await _debug_log(f"DEQUEUED from queue {id(queue)}: {remote_msg.text[:80]}")

            # Check if there is an active turn (lock is locked)
            if lock.locked():
                try:
                    # Treat as steer!
                    from novacode_cli.bootstrap.steering import SteeringInstruction

                    text = remote_msg.text.strip()
                    low = text.lower()
                    if low.startswith("/steer"):
                        text = text[len("/steer") :].strip()
                    elif (
                        low == "/goal" or low.startswith("/goal ")
                        or low == "/btw" or low.startswith("/btw ")
                    ):
                        # /btw runs concurrently; /goal records for the running
                        # task. Neither needs the turn lock.
                        await _handle_side_command(remote_msg, allow_kickoff=False)
                        queue.task_done()
                        continue
                    elif text.startswith("/"):
                        try:
                            await remote_msg.reply_fn(
                                "⏳ Busy with the current task — send "
                                "`/steer <text>` (or just text) to add to it."
                            )
                        except Exception:
                            pass
                        queue.task_done()
                        continue
                    if not text:
                        queue.task_done()
                        continue

                    if getattr(session_state, "steering_instructions", None) is None:
                        session_state.steering_instructions = []
                    si = SteeringInstruction(label="steer", instruction=text)
                    session_state.steering_instructions.append(si)

                    console.print(
                        f"  [cyan]↗ Steering injected from Remote ({remote_msg.platform.value}): {text}[/]"
                    )
                    try:
                        await remote_msg.reply_fn(f"↗ Added to the running task: {text}")
                    except Exception:
                        pass
                except Exception as ex:
                    logger.error(f"Error handling remote steer: {ex}")
                finally:
                    queue.task_done()
                continue

            # Start the run in a background task
            task = asyncio.create_task(run_turn(remote_msg))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

        except asyncio.CancelledError:
            logger.info("Remote message processor cancelled")
            for t in active_tasks:
                t.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            break
        except Exception as e:
            logger.error(f"Remote message processor error: {e}")
            await asyncio.sleep(1)


def _ai_message_text(msg: Any) -> str:
    """Flatten an AIMessage's content to text (str or content-block list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "").strip()
                if text:
                    parts.append(text)
            elif isinstance(part, str) and part.strip():
                parts.append(part.strip())
        return "\n\n".join(parts)
    return ""


def _extract_response(post_state: Any, pre_msg_count: int) -> str:
    """Return only the agent's FINAL answer from the state after execution.

    A tool-using turn produces many AI messages — the text in the intermediate
    ones is step-by-step narration between tool calls ("Now I'll…", "Let me…",
    "Good, now update…") which is noise in a chat (the live status line already
    shows *what* it's doing). So we return the **last** AI message that actually
    has text — the answer — not a concatenation of the whole play-by-play.
    """
    if post_state is None:
        return ""

    from langchain_core.messages import AIMessage

    messages = post_state.values.get("messages", [])
    new_messages = messages[pre_msg_count:]
    for msg in reversed(new_messages):
        if isinstance(msg, AIMessage):
            text = _ai_message_text(msg)
            if text:
                return text
    return ""

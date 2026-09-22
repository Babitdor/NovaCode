"""Conversation compaction and summarization for NovaCode-cli.

This module provides functionality to compress conversation history
by generating an intelligent summary that preserves key context.
"""

import json
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from novacode_cli.context import CompactionResult
from novacode_cli.prompts import render_template

# Summarization prompt template (loaded from Jinja)
# Template file: NovaCode_cli/prompts/summarization.jinja

# Compaction rewrites the whole conversation into ONE synthetic HumanMessage
# carrying the summary (see compact_conversation). It has to be a HumanMessage
# for the provider's turn ordering, but it is not something the user said — so
# any surface that renders history must skip it, or the entire summarized
# context appears in the transcript as a message the user supposedly typed.
COMPACTION_SUMMARY_MARKER = "[Conversation context — previous session summarized]"


def is_compaction_summary(text: str) -> bool:
    """True if *text* is the synthetic message compaction injects."""
    return text.lstrip().startswith(COMPACTION_SUMMARY_MARKER)


def _format_message_content(content: Any) -> str:
    """Format message content to a string.

    Handles both string content and content blocks (list of dicts).

    Args:
        content: The message content (str or list of content blocks)

    Returns:
        Formatted string representation
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # Handle content blocks (e.g., from Claude)
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "unknown_tool")
                    text_parts.append(f"[Called tool: {tool_name}]")
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts)

    return str(content)


def _message_parts(messages: list[BaseMessage]) -> list[str]:
    """Render each message to a compact ``ROLE: text`` line (per-message truncation)."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = _format_message_content(msg.content)
            if len(content) > 2000:
                content = content[:2000] + "... [truncated]"
            parts.append(f"USER: {content}")
        elif isinstance(msg, AIMessage):
            content = _format_message_content(msg.content)
            if len(content) > 2000:
                content = content[:2000] + "... [truncated]"
            # Tool calls live in .tool_calls, not .content — on a tool-calling
            # turn the content is usually empty, so without this the summarizer
            # never saw WHICH file was edited or WHAT command ran, and "Files
            # Modified" was guessed from truncated tool results.
            calls = [_render_tool_call(tc) for tc in getattr(msg, "tool_calls", None) or []]
            parts.append("ASSISTANT: " + "\n".join(filter(None, [content, *calls])))
        elif isinstance(msg, ToolMessage):
            content = _format_message_content(msg.content)
            if len(content) > 500:
                content = content[:500] + "... [truncated]"
            parts.append(f"TOOL({msg.name}): {content}")
    return parts


_TOOL_ARGS_CHARS = 300


def _render_tool_call(call: dict) -> str:
    """Render a tool call as ``-> name({args})`` with the args capped.

    Enough for the summarizer to know what was touched, not a copy of the payload.
    """
    args = json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
    if len(args) > _TOOL_ARGS_CHARS:
        args = args[:_TOOL_ARGS_CHARS] + "...}"
    return f"-> {call.get('name', 'tool')}({args})"


def _window_tokens(model: BaseChatModel, context_window: int | None = None) -> int:
    """Return the model's context window in tokens.

    The caller's value wins (the TUI passes the tracker's effective window,
    which accounts for Ollama ``num_ctx``); else the static table
    (``use_dynamic=False`` — no slow/flaky live query here).
    """
    if context_window and context_window > 0:
        return context_window
    name = getattr(model, "model_name", None) or getattr(model, "model", None) or ""
    try:
        from novacode_cli.context._analysis import get_context_window_size

        return get_context_window_size(str(name), use_dynamic=False)
    except Exception:  # noqa: BLE001
        return 8192


def _budget_chars(model: BaseChatModel, context_window: int | None = None) -> int:
    """Character budget for the summarizer's input, sized to the model's context
    window so a long conversation never overflows the summarization call itself.

    Reserves ~45% of the window for the prompt template + generated summary;
    rough 4-chars/token.
    """
    return max(8000, int(_window_tokens(model, context_window) * 0.55) * 4)


# The recent tail compaction keeps VERBATIM, as a share of the window, capped.
# Codex keeps ~20k tokens of recent messages; deepagents' own summarizer keeps
# 10% of the window. Summarizing the turn the agent is in the middle of is the
# lossiest possible cut — the exact error text, the file just edited, the
# user's latest wording all become a paraphrase.
KEEP_TAIL_FRACTION = 0.10
KEEP_TAIL_MAX_TOKENS = 20_000


def _message_chars(msg: BaseMessage) -> int:
    size = len(_format_message_content(msg.content))
    for call in getattr(msg, "tool_calls", None) or []:
        size += len(json.dumps(call.get("args") or {}, default=str))
    return size


def _split_tail(messages: list[BaseMessage], keep_tokens: int) -> int:
    """Index where the verbatim tail starts (``len(messages)`` = keep nothing).

    Cuts only at a real user message, so a tool call is never separated from
    its result, and takes the EARLIEST such cut whose tail fits the budget. A
    tail with a ToolMessage whose call is not in the tail is rejected as a
    second guard (a steer injected mid-turn is also a HumanMessage).
    """
    budget = keep_tokens * 4
    best = len(messages)
    size = 0
    for i in range(len(messages) - 1, 0, -1):  # index 0: nothing left to summarize
        size += _message_chars(messages[i])
        if size > budget:
            break
        msg = messages[i]
        if isinstance(msg, HumanMessage) and not is_compaction_summary(
            _format_message_content(msg.content)
        ):
            best = i
    tail = messages[best:]
    issued = {tc.get("id") for m in tail for tc in (getattr(m, "tool_calls", None) or [])}
    if any(isinstance(m, ToolMessage) and m.tool_call_id not in issued for m in tail):
        return len(messages)
    return best


def _rehydration_note() -> str:
    """Paths the agent touched, so it can re-read them just in time.

    Claude Code and Codex re-attach recently used files after compacting. Paths
    are the cheap form of that: the content is on disk, the summary only has
    to say where to look.
    """
    try:
        from novacode_cli.tracking.file_tracker import (
            get_modified_files,
            get_recently_read_files,
        )

        modified = get_modified_files()[-15:]
        read = [p for p in get_recently_read_files(15) if p not in modified]
    except Exception:  # noqa: BLE001 — a hint, never worth failing compaction
        return ""
    lines = []
    if modified:
        lines.append("- Modified: " + ", ".join(modified))
    if read:
        lines.append("- Read recently: " + ", ".join(read))
    if not lines:
        return ""
    return (
        "## Files touched this session\n"
        "Re-read a file before relying on its contents; the summary only "
        "describes it.\n" + "\n".join(lines)
    )


def _chunk_parts(parts: list[str], max_chars: int) -> list[list[str]]:
    """Group formatted parts into chunks each within ``max_chars``."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for p in parts:
        if len(p) > max_chars:  # a single oversized part — hard-truncate it
            p = p[:max_chars] + "... [truncated]"
        if cur and cur_len + len(p) + 2 > max_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p) + 2
    if cur:
        chunks.append(cur)
    return chunks


async def _summarize_text(
    model: BaseChatModel,
    conversation_text: str,
    focus_instructions: str | None,
    *,
    tail_kept: bool = False,
) -> str:
    """One summarization LLM call over ``conversation_text``."""
    if focus_instructions:
        focus_text = (
            f"\n**IMPORTANT - User requested focus on**: {focus_instructions}\n\n"
            "Make sure to especially preserve information related to this focus area.\n"
        )
    else:
        focus_text = ""
    prompt = render_template(
        "summarization.jinja",
        focus_instructions=focus_text,
        conversation=conversation_text,
        tail_kept=tail_kept,
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return _format_message_content(response.content)


def _answer_budget(budget: int, questions: str) -> int:
    """Conversation budget for the answer pass (reserve room for questions + template)."""
    return max(1000, budget - len(questions) - 500)


async def _generate_questions(model: BaseChatModel, summary: str) -> str:
    """One LLM call: read the summary and ask what important details are missing."""
    prompt = render_template("summarization_questions.jinja", summary=summary)
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return _format_message_content(response.content).strip()


async def _answer_questions(
    model: BaseChatModel, questions: str, parts: list[str], budget: int
) -> str:
    """Answer the clarifying questions from the full conversation.

    Fits in one call when the conversation does; otherwise answers per chunk
    and merges the per-chunk answers into one consolidated Q&A block.
    """
    text = "\n\n".join(parts)
    q_budget = _answer_budget(budget, questions)
    if len(text) <= q_budget:
        prompt = render_template(
            "summarization_answers.jinja", questions=questions, conversation=text
        )
        response = await model.ainvoke([HumanMessage(content=prompt)])
        return _format_message_content(response.content).strip()

    per_chunk: list[str] = []
    for chunk in _chunk_parts(parts, q_budget):
        prompt = render_template(
            "summarization_answers.jinja",
            questions=questions,
            conversation="\n\n".join(chunk),
        )
        response = await model.ainvoke([HumanMessage(content=prompt)])
        per_chunk.append(_format_message_content(response.content).strip())

    # Merge: consolidate the per-chunk answers into one Q&A block (bounded).
    merged = "\n\n".join(
        f"## Excerpt {i + 1} of {len(per_chunk)}\n{a}" for i, a in enumerate(per_chunk)
    )
    prompt = render_template(
        "summarization_answers.jinja",
        questions=questions,
        conversation=merged[:q_budget],
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return _format_message_content(response.content).strip()


async def _qa_refine(
    model: BaseChatModel, summary: str, parts: list[str], budget: int
) -> str:
    """Gap-fill a lossy summary: ask what it missed, restore it from the source.

    Best-effort — any failure returns ``""`` so compaction falls back to the
    plain summary. Returns the Q&A block to append, or ``""`` when nothing
    is missing.
    """
    try:
        questions = await _generate_questions(model, summary)
        if not questions or "NONE" in questions.upper():
            return ""
        answers = await _answer_questions(model, questions, parts, budget)
        if not answers.strip():
            return ""
        return f"## Clarifying Q&A\n\n{answers.strip()}"
    except Exception:  # noqa: BLE001 — refinement must never break compaction
        return ""


async def summarize_conversation(
    model: BaseChatModel,
    messages: list[BaseMessage],
    focus_instructions: str | None = None,
    context_window: int | None = None,
    *,
    tail_kept: bool = False,
) -> str:
    """Summarize a conversation, budgeted to the model's context window.

    Short conversations are summarized in a single call. Long ones (that would
    overflow the summarizer's own window — exactly when compaction matters most)
    are summarized **hierarchically**: chunk → summarize each chunk → summarize
    the chunk-summaries into one cohesive summary, recursing if still too large.
    The hierarchical path then runs a **Q&A gap-filling pass** (Meta-Harness
    port): a second call reads the summary and asks what important details are
    missing, and a third answers those questions from the full conversation —
    recovering details a single lossy pass drops. Best-effort: any failure falls
    back to the plain summary.

    Args:
        model: The LLM to use for summarization
        messages: The conversation messages to summarize
        focus_instructions: Optional focus instructions from the user
        context_window: Optional explicit context window (tokens); the TUI passes
            the token tracker's effective window.
        tail_kept: The most recent messages stay verbatim after the summary, so
            the summary covers only what precedes them.

    Returns:
        The summarized conversation as a string

    Raises:
        Exception: If the LLM call fails
    """
    parts = _message_parts(messages)
    budget = _budget_chars(model, context_window)
    text = "\n\n".join(parts)

    if len(text) <= budget:
        return await _summarize_text(model, text, focus_instructions, tail_kept=tail_kept)

    # Too big for one call → hierarchical summarization.
    chunks = _chunk_parts(parts, budget)
    summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        s = await _summarize_text(model, "\n\n".join(chunk), focus_instructions)
        summaries.append(f"## Part {i + 1} of {len(chunks)}\n{s}")
    combined = "\n\n".join(summaries)

    # If even the combined chunk-summaries are too large, collapse them further.
    depth = 0
    while len(combined) > budget and depth < 3:
        depth += 1
        summaries = [
            await _summarize_text(model, "\n\n".join(group), focus_instructions)
            for group in _chunk_parts(combined.split("\n\n"), budget)
        ]
        combined = "\n\n".join(summaries)

    # Final pass: one cohesive summary from the collapsed material.
    final = await _summarize_text(model, combined[:budget], focus_instructions, tail_kept=tail_kept)

    # Q&A gap-filling (Meta-Harness port): the hierarchical path is lossy — ask
    # what the summary missed and restore it from the full conversation. Only
    # here: short single-pass summaries are already high-fidelity.
    qa = await _qa_refine(model, final, parts, budget)
    if qa:
        final = f"{final}\n\n{qa}"
    return final


async def compact_conversation(
    agent: Any,
    model: BaseChatModel,
    thread_id: str,
    focus_instructions: str | None = None,
    context_window: int | None = None,
    agent_dir: Path | None = None,
) -> CompactionResult:
    """Compact a conversation by summarizing and replacing history.

    This function:
    1. Retrieves the current conversation history
    2. Generates a summary using the LLM
    3. Replaces the conversation with a single summary message

    Args:
        agent: The LangGraph agent
        model: The LLM model for summarization
        thread_id: The thread/session ID
        focus_instructions: Optional user instructions for what to preserve
        context_window: Model context window (tokens) to budget the summary to
        agent_dir: If given, the compaction summary is also persisted as a
            durable memory lesson (``memories/session-summary.md``) so the
            conversation's knowledge survives the rewrite.

    Returns:
        CompactionResult with details of the compaction operation
    """
    config = {"configurable": {"thread_id": thread_id}}

    try:
        # Get current state
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])

        if not messages:
            return CompactionResult(
                success=False,
                original_tokens=0,
                new_tokens=0,
                tokens_saved=0,
                messages_before=0,
                messages_after=0,
                summary="",
                error="No conversation history to compact.",
            )

        messages_before = len(messages)

        # Count original tokens using the model's tokenizer when available,
        # falling back to the rough 4-chars-per-token approximation.
        original_text = " ".join(
            _format_message_content(msg.content) for msg in messages if hasattr(msg, "content")
        )
        try:
            _orig = model.get_num_tokens_from_messages([HumanMessage(content=original_text)])
            original_tokens = _orig if isinstance(_orig, int) else len(original_text) // 4
        except Exception:
            original_tokens = len(original_text) // 4

        # Keep the recent tail verbatim; summarize only what precedes it.
        keep_tokens = min(
            KEEP_TAIL_MAX_TOKENS,
            int(_window_tokens(model, context_window) * KEEP_TAIL_FRACTION),
        )
        cut = _split_tail(messages, keep_tokens)
        head, tail = messages[:cut], messages[cut:]

        # Generate summary (budgeted to the model's context window)
        summary = await summarize_conversation(
            model,
            head,
            focus_instructions,
            context_window=context_window,
            tail_kept=bool(tail),
        )

        # Replace the conversation in a single atomic update: REMOVE_ALL then
        # summary + tail, so the summary lands BEFORE the tail (a plain
        # RemoveMessage per id would append it after) and the state never
        # passes through an invalid intermediate (ToolMessages with no
        # AIMessage), which crashes langchain's _fetch_last_ai_and_tool_messages.
        framing = (
            "The earlier part of this conversation was compacted into the summary "
            "below"
            + ("; the most recent messages follow it verbatim" if tail else "")
            + ". Continue the work from where it stopped. Do not acknowledge or "
            "restate this summary."
        )
        body = "\n\n".join(filter(None, [framing, summary, _rehydration_note()]))
        summary_message = HumanMessage(content=f"{COMPACTION_SUMMARY_MARKER}\n\n{body}")
        # Also clear any prior auto-summarization event. deepagents'
        # SummarizationMiddleware reconstructs the effective message list from
        # `_summarization_event` (a cutoff index into the OLD message list); if we
        # rewrite messages without clearing it, the next turn would slice the new
        # list at a stale index and corrupt context. Resetting it makes the fresh
        # summary the whole context.
        new_messages = [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_message, *tail]
        update_values: dict[str, Any] = {
            "messages": new_messages,
            "_summarization_event": None,
        }
        try:
            await agent.aupdate_state(config=config, values=update_values, as_node="model")
        except Exception:
            # Older graphs without the summarization state key reject the extra
            # field; retry with just the message rewrite.
            await agent.aupdate_state(
                config=config,
                values={"messages": new_messages},
                as_node="model",
            )

        # Count new tokens using the model's tokenizer when available.
        try:
            _new = model.get_num_tokens_from_messages([summary_message, *tail])
            new_tokens = _new if isinstance(_new, int) else len(body) // 4
        except Exception:
            new_tokens = (len(body) + sum(_message_chars(m) for m in tail)) // 4

        # Persist the summary as a durable memory lesson so the conversation's
        # knowledge survives the rewrite (best-effort; never fails compaction).
        learnings = ""
        if agent_dir is not None:
            try:
                from novacode_cli.hermes.memory_tiers import record_lesson

                # The marker travels WITH the artifact: a derived summary is
                # injected into future prompts, so it must carry its own
                # provenance rather than relying on an instruction elsewhere to
                # establish precedence. Unlabelled, it reads as verified fact.
                record_lesson(
                    agent_dir,
                    "session-summary",
                    "[derived summary, not a verified record — may be incomplete; "
                    "observed reality wins on any conflict]\n\n" + summary,
                )
                learnings = summary
            except Exception:  # noqa: BLE001 — memory persistence is best-effort
                learnings = ""

        return CompactionResult(
            success=True,
            original_tokens=original_tokens,
            new_tokens=new_tokens,
            tokens_saved=max(0, original_tokens - new_tokens),
            messages_before=messages_before,
            messages_after=1 + len(tail),
            summary=summary,
            learnings=learnings,
        )

    except Exception as e:
        return CompactionResult(
            success=False,
            original_tokens=0,
            new_tokens=0,
            tokens_saved=0,
            messages_before=0,
            messages_after=0,
            summary="",
            error=str(e),
        )

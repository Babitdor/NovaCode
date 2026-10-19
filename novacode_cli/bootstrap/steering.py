"""Steering middleware for injecting persistent user guidance into every model call.

Steering lets the user add standing instructions that persist across turns,
unlike regular chat messages which are "one and done."  Examples:

    /steer focus on the database layer
    /steer never modify test files without asking first
    /steer prioritize backward compatibility
    /steer use async patterns everywhere

These instructions are injected into every model call as a dedicated
section in the system prompt, ensuring the agent obeys them on every
turn — not just the one where they were typed.

The steering state is held in ``session_state.steering_instructions``,
a list of ``(label, instruction)`` tuples.  The ``/steer`` slash command
is the primary interface for managing them.

Architecture
-----------
``SteeringMiddleware`` sits in the agent middleware stack alongside
``GraphContextMiddleware``, ``ShellMiddleware``, etc.  It intercepts
every ``awrap_model_call`` and prepends the current steering instructions
to the system prompt, right after the base prompt but before any other
middleware injections.

The injection format is::

    [Steering Instructions]
    - focus on the database layer
    - never modify test files without asking first
    - prioritize backward compatibility
    [/Steering Instructions]

This is deliberately verbose — the agent sees these on every call, so
they act as persistent guardrails rather than one-off suggestions.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

# ── Context-window safety ──────────────────────────────────────────
# If the system prompt (after ALL middleware injections) exceeds this
# fraction of the model's context window, skip steering to avoid
# silently truncating the model's output. Hardcoded as a class-level
# constant so subclasses can override.
_INJECTION_CTX_MARGIN = 0.80  # 80% — generous margin for messages + output
_STEERING_SENTINEL = "[/User Steering]"


# ---------------------------------------------------------------------------
# Steering instruction storage
# ---------------------------------------------------------------------------


_uid_counter = itertools.count()


class SteeringInstruction:
    """A single steering instruction with a label for display.

    Attributes:
        label: Short human-readable label (e.g., "focus", "constraint", "style").
        instruction: The full instruction text.
        uid: Process-unique id, used to deliver each steer as a live message
            exactly once (stable across GC, unlike ``id()``).
    """

    __slots__ = ("label", "instruction", "uid", "consumed", "live")

    def __init__(self, label: str, instruction: str, *, live: bool = False) -> None:
        self.label = label
        self.instruction = instruction
        self.uid = next(_uid_counter)
        self.consumed = False
        #: A message typed while the agent worked (a queued message), not a
        #: standing /steer rule: it joins the conversation at the next step.
        self.live = live

    def __repr__(self) -> str:
        return f"SteeringInstruction({self.label!r}, {self.instruction!r})"


#: Marks a queued message in the conversation (``additional_kwargs``).
QUEUED_SOURCE = "queued_message"


def queued_message(text: str) -> HumanMessage:
    """A message typed while the agent worked, as it joins the conversation."""
    return HumanMessage(
        "[Sent by the user while you were working: incorporate it into the "
        f"current task now]\n{text}",
        additional_kwargs={"lc_source": QUEUED_SOURCE, "queued_text": text},
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class SteeringMiddleware(AgentMiddleware):
    """Inject persistent steering instructions into every model call.

    The middleware holds a reference to a shared list of
    ``SteeringInstruction`` objects.  The same list is stored in
    ``session_state.steering_instructions``, so updates via the ``/steer``
    command are immediately visible to the middleware — no state
    synchronization needed.

    When no steering instructions are active, this middleware is a no-op.
    """

    state_schema = AgentState

    def __init__(
        self,
        instructions: list[SteeringInstruction] | None = None,
        enabled: bool = True,
    ) -> None:
        """Initialize the steering middleware.

        Args:
            instructions: Shared list of steering instructions.  If None,
                creates an empty list internally (not connected to session state).
            enabled: If False, this middleware is a no-op.
        """
        self._enabled = enabled
        self._instructions: list[SteeringInstruction] = (
            instructions if instructions is not None else []
        )
        # uids of instructions already surfaced as a live user message, so each
        # new steer is delivered as an actionable message exactly once (then
        # persists via the system prompt).
        self._delivered: set[int] = set()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate for a text blob (~3 chars/token for code-heavy text)."""
        return max(1, len(text) // 3)

    def _lookup_context_window(self, request: ModelRequest) -> int | None:
        """Return the model's context-window size in tokens, or None if unknown."""
        # 1) Check model.profile (seeded by _seed_summarization_profile in core_agent.py)
        try:
            profile = getattr(request.model, "profile", None) or {}
            max_tokens = profile.get("max_input_tokens")
            if max_tokens and int(max_tokens) > 0:
                return int(max_tokens)
        except Exception:  # noqa: BLE001
            pass
        # 2) Fall back to ContextManager lookup
        try:
            model_name = (
                getattr(request.model, "model_name", None)
                or getattr(request.model, "model", None)
                or ""
            )
            if model_name:
                from novacode_cli.context.manager import ContextManager

                cm = ContextManager(model_name)
                return cm.window_size()
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _messages_chars(messages: Any) -> int:
        """Total character count of the conversation messages' content."""
        total = 0
        for m in messages or []:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        total += len(part)
                    elif isinstance(part, dict):
                        total += len(str(part.get("text", "")))
        return total

    def _prompt_within_margin(self, request: ModelRequest, added_chars: int) -> bool:
        """True if the *full* input (system + history + steering) fits the margin.

        The real input the model receives is the system prompt **plus the whole
        conversation**, so a steering block that fits the system prompt alone can
        still push a long chat over the window — which truncates the model's
        answer mid-sentence. We therefore count the messages too (the previous
        check looked only at the system prompt). When the window is unknown
        (no model profile / ContextManager entry) we assume yes rather than
        silently dropping steering.
        """
        window = self._lookup_context_window(request)
        if window is None:
            return True  # can't measure → don't block
        system_chars = len(request.system_prompt or "")
        messages_chars = self._messages_chars(getattr(request, "messages", None))
        total = max(1, (system_chars + messages_chars + max(0, added_chars)) // 3)
        margin = int(window * _INJECTION_CTX_MARGIN)
        if total >= margin:
            logger.warning(
                "Steering injection skipped: input ~%s tokens (system + history + "
                "steering) exceeds %s%% of %s-token context window (%s tokens)",
                total,
                int(_INJECTION_CTX_MARGIN * 100),
                window,
                margin,
            )
            return False
        return True

    def _inject(self, request: ModelRequest) -> ModelRequest:
        """Inject steering instructions into this model call.

        Two layers:
        * a persistent block appended to the system prompt (standing guardrails
          the agent must honor on every step), and
        * for any *newly added* steer (e.g. typed while the agent was working),
          a one-time HumanMessage so the agent actively reads and incorporates it
          into the task in progress — a passive system-prompt line mid-tool-loop
          is easy to overlook.

        Guards:
        * **Sentinel** — skips system-prompt injection when the block is already
          present (accumulating copies on every model call bloats the prompt).
        * **Context margin** — skips system-prompt injection when the prompt after
          injection would exceed 80 % of the model's context window (overflow
          silently truncates the model's output).
        * The one-time nudge HumanMessage for *new* steers still fires in both
          cases — even when the standing block is skipped, newly added instructions
          reach the model via an actionable in-band message.
        """
        if not self._enabled:
            return request

        # Queued messages are in the conversation already (before_model).
        instructions = [si for si in self._instructions if not si.live]
        if not instructions:
            return request

        # ── Build the steering block ──────────────────────────────────
        lines = [
            f"- [{si.label}] {si.instruction}" if si.label else f"- {si.instruction}"
            for si in instructions
        ]
        block = (
            "[User Steering — standing instructions from the user that you MUST "
            "follow on every step]\n" + "\n".join(lines) + "\n[/User Steering]"
        )

        overrides: dict[str, Any] = {}

        # ── Guard 1: Sentinel — skip if already injected ──────────────
        system_prompt = request.system_prompt
        already_injected = system_prompt is not None and _STEERING_SENTINEL in system_prompt

        if not already_injected and self._prompt_within_margin(request, len(block)):
            new_prompt = (system_prompt + "\n\n" + block) if system_prompt else block
            overrides["system_message"] = SystemMessage(new_prompt)

        # ── Guard 2 (always): deliver newly-added steers as a live msg ─
        new = [si for si in instructions if si.uid not in self._delivered]
        if new:
            for si in new:
                self._delivered.add(si.uid)
                si.consumed = True
            nudge = "\n".join(f"- {si.instruction}" for si in new)
            msg = HumanMessage(
                "[The user added the following while you were working — "
                "incorporate it into your current task now, in addition to what "
                "you're already doing]\n" + nudge
            )
            overrides["messages"] = [*request.messages, msg]

        if not overrides:
            return request
        return request.override(**overrides)

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        """Record messages typed while the agent worked, at its next step.

        A queued message becomes a user message in the conversation (as in
        Claude Code), not just text in one request: later turns, compaction
        summaries and /resume then all know the user said it.
        """
        if not self._enabled:
            return None
        new = [si for si in self._instructions if si.live and si.uid not in self._delivered]
        if not new:
            return None
        for si in new:
            self._delivered.add(si.uid)
            si.consumed = True
            _announce_pickup(si.instruction)
        return {"messages": [queued_message(si.instruction) for si in new]}

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        return self.before_model(state, runtime)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        """Deliver late user input before a final answer ends the graph turn."""
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            # Tool results must pair with their calls before adding user input.
            return None
        update = self.before_model(state, runtime)
        return {**update, "jump_to": "model"} if update else None

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        """Async version of the late-message continuation check."""
        return self.after_model(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject steering (sync)."""
        return handler(self._inject(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Inject steering (async)."""
        return await handler(self._inject(request))


# ---------------------------------------------------------------------------
# Formatting helpers (used by /steer command)
# ---------------------------------------------------------------------------


def _announce_pickup(text: str) -> None:
    """Tell the UI a queued message reached the agent (best effort)."""
    try:
        from novacode_cli.events import nova_event_log

        short = " ".join(text.split())
        short = short if len(short) <= 80 else short[:79] + "…"
        nova_event_log.append(("queued_pickup", "✓", "dim", f"Picked up: {short}"))
    except Exception:  # noqa: BLE001 - a notice is never worth a failed step
        logger.debug("queued-message notice failed", exc_info=True)


def format_steering_status(instructions: list[SteeringInstruction]) -> str:
    """Format the current steering instructions as a Rich-displayable summary.

    Args:
        instructions: List of active steering instructions.

    Returns:
        Formatted string for console display.  Empty string if none.
    """
    if not instructions:
        return "[dim]No steering instructions active.[/dim]"

    lines = [f"[cyan]{len(instructions)} steering instruction(s) active:[/cyan]"]
    for i, si in enumerate(instructions, 1):
        if si.label:
            lines.append(f"  [dim]{i}.[/dim] [bold]{si.label}[/bold]: {si.instruction}")
        else:
            lines.append(f"  [dim]{i}.[/dim] {si.instruction}")

    return "\n".join(lines)


def classify_instruction(text: str) -> str:
    """Classify a steering instruction into a category label.

    This is a simple heuristic that assigns a label based on keywords
    in the instruction text.  The label is used for display and
    organization, not for any logic.

    Args:
        text: The raw instruction text.

    Returns:
        A short label string (e.g., "focus", "constraint", "style", "priority").
    """
    text_lower = text.lower()

    # Constraint signals
    constraint_keywords = [
        "never",
        "don't",
        "dont",
        "do not",
        "must not",
        "cannot",
        "avoid",
        "refrain",
        "forbidden",
        "disallow",
        "no ",
    ]
    if any(kw in text_lower for kw in constraint_keywords):
        return "constraint"

    # Focus signals
    focus_keywords = [
        "focus on",
        "concentrate on",
        "prioritize",
        "emphasize",
        "pay attention to",
        "concentrate",
        "center on",
    ]
    if any(kw in text_lower for kw in focus_keywords):
        return "focus"

    # Priority signals
    priority_keywords = [
        "always",
        "make sure",
        "ensure",
        "guarantee",
        "important",
        "critical",
        "essential",
        "must",
    ]
    if any(kw in text_lower for kw in priority_keywords):
        return "priority"

    # Style signals
    style_keywords = [
        "use async",
        "use ",
        "prefer ",
        "style",
        "format",
        "pattern",
        "verbose",
        "concise",
        "brief",
        "detailed",
    ]
    if any(kw in text_lower for kw in style_keywords):
        return "style"

    return "guide"

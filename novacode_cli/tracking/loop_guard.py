"""Loop-guard middleware: break stuck, repeating tool calls.

Models sometimes get stuck firing the *exact same* tool call over and over —
e.g. ``grep("morphological|knowledge|...")`` returning "no matches" 18 times in
a row — without ever updating their plan. Each repeat makes no progress, burns
tokens and wall-clock time, and the agent never escapes on its own.

This middleware watches a sliding window of recent tool calls. When the same
tool is invoked with the same arguments and returns the same result
``threshold`` times within that window, the next identical call is
short-circuited with an error message that tells the model to stop repeating
and change approach.

The window — rather than only the immediately-previous call — is what lets the
guard catch the common loop shape ``grep → think → grep → think → grep …``,
where a harmless intervening call would otherwise reset a strict consecutive
streak forever and the loop never gets broken.

The detection stays conservative to avoid false positives:

- Intervening *different* calls are skipped, not counted — but they do not reset
  the count for the repeating signature. Heavy interleaving still ages old
  repeats out of the bounded window, so a non-tight loop is left alone.
- The *result* must also be identical. Re-running a test command that flips
  from failing to passing is NOT a loop; a search that always says "no matches"
  is. A changed result for the same args stops the count — progress, not a loop.
"""

import hashlib
import difflib
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

if TYPE_CHECKING:
    from langchain.tools import BaseTool


#: Temperature used for a model call made while the model is repeating itself.
_UNSTICK_TEMPERATURE = 0.7

#: How alike two calls' arguments must be to count as one call restated, when
#: both returned the same result. A long identical result is strong evidence on
#: its own, so the arguments may differ a little...
_NEAR_MIN_RESULT_CHARS = 200
_NEAR_ARGS_RATIO = 0.9
#: ...but a short one ("ok", "NO ALERT") is what honest trial and error prints
#: too, so there the calls must be long and all but identical: a rename, not a
#: different attempt.
_NEAR_ARGS_RATIO_SHORT_RESULT = 0.99
_NEAR_MIN_ARGS_CHARS = 300


def _canonical_args(tool_call: dict) -> str:
    """Stable string key for a tool call's name + arguments."""
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    try:
        args_str = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_str = repr(args)
    return f"{name}\x00{args_str}"


def _restates(previous_args: str, args: str, long_result: bool) -> bool:  # noqa: FBT001
    """Whether ``args`` is ``previous_args`` again, give or take a rename."""
    if long_result:
        needed = _NEAR_ARGS_RATIO
    elif min(len(previous_args), len(args)) >= _NEAR_MIN_ARGS_CHARS:
        needed = _NEAR_ARGS_RATIO_SHORT_RESULT
    else:
        return previous_args == args
    matcher = difflib.SequenceMatcher(None, previous_args, args, autojunk=False)
    return matcher.real_quick_ratio() >= needed and matcher.ratio() >= needed


def _result_text(result: ToolMessage | Command) -> str | None:
    """Extract comparable text from a tool result, or None if not guardable."""
    if not isinstance(result, ToolMessage):
        return None  # Command (state update) — leave it alone
    content = result.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


class LoopGuardMiddleware(AgentMiddleware):
    """Short-circuit a tool call that has stalled in an identical-repeat loop.

    Blocking the call is not enough on its own: a truly stuck model will keep
    re-emitting the *same* call and, if it always gets back the *same* block
    message, nothing in its context changes — so it loops on the guard itself
    (the "LOOP STOPPED … LOOP STOPPED …" wall). Two mechanisms break that:

    - **Escalation.** Each successive block of the same signature returns a
      *different*, stronger message. A changing tool result is the single most
      reliable way to perturb a degenerate loop, so varying the text gives the
      model a real chance to choose a different action.
    - **Hard stop.** If the model ignores ``escalate_after`` escalations anyway,
      the guard ends the turn with a final message rather than letting the
      wall scroll forever. The stop is made in ``before_model`` (``jump_to:
      "end"``): a ``Command(goto=END)`` returned from the tool node does not
      end the turn, because the node's own edge back to the model still fires
      — one eval trial repeated a single command 318 times through 80 "halts".

    Args:
        threshold: Number of identical (tool, args, result) calls — within the
            sliding window, intervening other calls ignored — to allow before
            blocking the next one. Default 3 — i.e. the 4th repeat is blocked.
        window: How many recent guardable results to retain when looking for
            repeats. Must comfortably exceed ``threshold`` so that a few
            interleaved calls between repeats don't age the streak out. Default
            12 (holds a ``grep``/``think`` alternation of 6 repeats).
        escalate_after: How many escalating blocks of the *same* signature to
            emit before hard-stopping the turn. Default 3 — i.e. the model gets
            three increasingly forceful warnings, then the 4th block ends the
            turn. A real call executing in between resets this.
    """

    def __init__(
        self, threshold: int = 3, window: int = 12, escalate_after: int = 3
    ) -> None:
        """Initialise the guard with the repeat ``threshold`` and ``window``."""
        super().__init__()
        self.threshold = threshold
        self.escalate_after = escalate_after
        self.tools: list[BaseTool] = []  # no additional tools
        self._history: deque[tuple[str, str]] = deque(maxlen=window)
        # Consecutive-block escalation state for the currently-stuck signature.
        self._blocked_sig: str | None = None
        self._block_streak: int = 0
        self._halt = False  # set by _terminate, acted on in before_model
        self._repeats = 0  # identical (call, result) count for the latest call
        # (tool, result hash, consecutive count, last args, result was long) for
        # the run of restated calls with one result that the latest call is in.
        self._near: tuple[str, str, int, str, bool] | None = None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict | None:  # noqa: ANN001, ARG002
        """End the turn if the last tool step hit the hard stop."""
        if not self._halt:
            return None
        self._halt = False
        note = (
            "I stopped: I was repeating the same tool call without getting anywhere. "
            "The work so far is as the tool results above show it."
        )
        return {"messages": [AIMessage(content=note)], "jump_to": "end"}

    async def abefore_model(self, state, runtime) -> dict | None:  # noqa: ANN001
        """(async) End the turn if the last tool step hit the hard stop."""
        return self.before_model(state, runtime)

    def _unstick(self, request):  # noqa: ANN001, ANN202
        """Let a model that is repeating itself sample its way out.

        At temperature 0 the reply to a context is fixed, and a context that
        ends in the same call and the same result again is nearly the same
        context — so the model emits the same call again, warnings or not.
        Only applied to a model configured with a low temperature, which shows
        it accepts the setting at all.
        """
        stuck = self._block_streak or self._repeats >= 2  # noqa: PLR2004
        base = getattr(request.model, "temperature", None)
        if not stuck or base is None or base >= _UNSTICK_TEMPERATURE:
            return request
        settings = {**(request.model_settings or {}), "temperature": _UNSTICK_TEMPERATURE}
        return request.override(model_settings=settings)

    def wrap_model_call(self, request, handler):  # noqa: ANN001, ANN201
        """Raise the temperature for this call if the model is repeating itself."""
        return handler(self._unstick(request))

    async def awrap_model_call(self, request, handler):  # noqa: ANN001, ANN201
        """(async) Raise the temperature if the model is repeating itself."""
        return await handler(self._unstick(request))

    def _near_loop(self, sig: str) -> str | None:
        """A stable signature if ``sig`` restates a call that keeps returning the same thing.

        A stuck model does not always repeat itself exactly. In two eval trials
        it rewrote one script again and again under a new file name each time
        (``analyze2.pl`` ... ``analyze170.pl``; ``expCB.py``, ``expCC.py`` ...)
        and got the identical output 170 and 274 times — invisible to a guard
        that compares arguments exactly.
        """
        if self._near is None:
            return None
        name, result_hash, count, last_args, long_result = self._near
        tool, _, args = sig.partition("\x00")
        if tool != name or count < self.threshold or not _restates(last_args, args, long_result):
            return None
        return f"{name}\x00~same-result~{result_hash}"

    def _repeat_count(self, sig: str) -> int:
        """Count identical (sig, result) repeats for ``sig`` within the window.

        Walks newest→oldest. Intervening calls with a *different* signature are
        skipped (so an interleaved think/read between identical greps does not
        reset the count). Counting stops as soon as ``sig`` is found to have
        produced a *different* result — a changed result means progress.
        """
        anchor_hash: str | None = None
        count = 0
        for past_sig, past_hash in reversed(self._history):
            if past_sig != sig:
                continue
            if anchor_hash is None:
                anchor_hash = past_hash
            if past_hash != anchor_hash:
                break
            count += 1
        return count

    def _on_blocked(
        self, sig: str, request: ToolCallRequest, count: int
    ) -> ToolMessage:
        """Handle a blocked call: escalate the message, then hard-stop the turn.

        Tracks how many times *this same signature* has been blocked in a row.
        Each block returns a stronger, *different* message (so the model's
        context actually changes); once ``escalate_after`` is exceeded the turn
        is ended outright.
        """
        if sig == self._blocked_sig:
            self._block_streak += 1
        else:
            self._blocked_sig = sig
            self._block_streak = 1

        if self._block_streak > self.escalate_after:
            return self._terminate(request, count)
        return self._blocked_message(request, count, self._block_streak)

    def _blocked_message(
        self, request: ToolCallRequest, count: int, streak: int
    ) -> ToolMessage:
        tool_call = request.tool_call
        name = tool_call.get("name", "this tool")
        if streak == 1:
            guidance = (
                "Do NOT call it again with the same arguments. Instead:\n"
                "- Change the approach — broaden/narrow the query, try a different "
                "tool, or look in a different location.\n"
                "- If you have enough information, stop searching and answer.\n"
                "- If the thing genuinely does not exist, say so and move on."
            )
        else:
            guidance = (
                f"You have repeated this identical call {streak} times AFTER being "
                f"told to stop — this is a hard loop. On your VERY NEXT step you "
                f"must do something different:\n"
                f"- call a DIFFERENT tool, or use DIFFERENT arguments (a different "
                f"pattern, path, or scope), OR\n"
                f"- stop calling tools and reply to the user with what you already "
                f"know — including that `{name}` found nothing.\n"
                f"Calling `{name}` with these same arguments again is forbidden and "
                f"will keep failing."
            )
        return ToolMessage(
            content=(
                f"**LOOP STOPPED** (repeat #{count + streak}): `{name}` was already "
                f"called {count} times with the same (or nearly the same) arguments and got "
                f"the identical result each time. Repeating it will not produce anything new.\n\n"
                f"{guidance}"
            ),
            tool_call_id=tool_call.get("id", ""),
            status="error",
        )

    def _terminate(self, request: ToolCallRequest, count: int) -> ToolMessage:
        """End the agent turn after the model ignored every escalation."""
        tool_call = request.tool_call
        name = tool_call.get("name", "this tool")
        msg = ToolMessage(
            content=(
                f"**LOOP HALTED**: `{name}` was called with identical arguments "
                f"{count} times and the loop continued through "
                f"{self.escalate_after} warnings. Ending this turn to stop the "
                f"runaway loop. Review the request and try a genuinely different "
                f"approach."
            ),
            tool_call_id=tool_call.get("id", ""),
            status="error",
        )
        # Reset so a later, legitimately-repeating signature starts fresh.
        self._reset_block_state()
        self._halt = True
        return msg

    def _reset_block_state(self) -> None:
        """Clear consecutive-block escalation tracking (progress was made)."""
        self._blocked_sig = None
        self._block_streak = 0

    def _record(self, sig: str, result: ToolMessage | Command) -> ToolMessage | Command:
        """Append an executed result to the window, if it is guardable.

        From the second identical (call, result) on, the result is returned with
        a note saying so. Waiting for the block at ``threshold`` leaves the
        model several turns of unchanged context to settle into the loop.
        """
        text = _result_text(result)
        if text is None:
            # Non-guardable result (Command) — don't record; it can't anchor a
            # loop and shouldn't occupy a window slot.
            return result
        result_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        self._history.append((sig, result_hash))
        tool, _, args = sig.partition("\x00")
        long_result = len(text) >= _NEAR_MIN_RESULT_CHARS
        run = 1
        if self._near and self._near[:2] == (tool, result_hash) and _restates(self._near[3], args, long_result):
            run = self._near[2] + 1
        self._near = (tool, result_hash, run, args, long_result)
        self._repeats = max(self._repeat_count(sig), run)
        if self._repeats < 2 or not isinstance(result.content, str):  # noqa: PLR2004
            return result
        note = (
            f"\n\n[note: this is the same result as the last {self._repeats - 1} time(s) — "
            f"{self._repeats} in a row. Running the same thing again, or the same thing under "
            "another name, will not change it — do something different.]"
        )
        return result.model_copy(update={"content": result.content + note})

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Short-circuit the call if it has stalled in an identical-repeat loop."""
        sig = _canonical_args(request.tool_call)
        count = self._repeat_count(sig)
        if count >= self.threshold:
            return self._on_blocked(sig, request, count)
        near = self._near_loop(sig)
        if near:
            return self._on_blocked(near, request, self._near[2])
        # A real call is running — the model made a different move; clear any
        # in-progress block escalation so it doesn't bleed into a future loop.
        self._reset_block_state()
        return self._record(sig, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """(async) Short-circuit the call if it has stalled in a repeat loop."""
        sig = _canonical_args(request.tool_call)
        count = self._repeat_count(sig)
        if count >= self.threshold:
            return self._on_blocked(sig, request, count)
        near = self._near_loop(sig)
        if near:
            return self._on_blocked(near, request, self._near[2])
        # A real call is running — clear any in-progress block escalation.
        self._reset_block_state()
        return self._record(sig, await handler(request))


class TextRepetitionGuard:
    """Detect degenerate self-repetition in a model's streamed prose.

    The other failure shape: the model stops making tool calls and just
    re-emits the same paragraphs verbatim, forever ("Let me also check the X
    element. … Good." on a cycle). ``LoopGuardMiddleware`` never sees it — no
    tool ever runs — and the stream has no natural end, so the turn only stops
    when something dies.

    Detection is on *repeated blocks*, not repeated lines: a sliding window of
    ``window`` consecutive substantial lines is hashed, and the same window
    recurring ``threshold`` times means the model is cycling rather than
    writing. Requiring a multi-line block keeps legitimately repetitive output
    (boilerplate code, table rows, a wrapped list) from tripping it.

    Feed it every streamed text/reasoning delta; it buffers partial lines.
    """

    def __init__(
        self, *, threshold: int = 4, window: int = 3, min_line_chars: int = 40
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.min_line_chars = min_line_chars
        self.tripped = False
        self._partial = ""
        self._recent: deque[int] = deque(maxlen=window)
        self._seen: dict[tuple[int, ...], int] = {}

    def note_progress(self) -> None:
        """Record that the turn did real work (a tool call), clearing the history.

        Repetition only means "stuck" when nothing else is happening. A tool
        call is proof the turn is advancing, so blocks seen before it must not
        count toward blocks seen after it. Without this, a long productive turn
        whose narration repeats around each step ("Now I will apply the
        change… / I will read the surrounding context…") accumulated repeats
        across unrelated steps and was killed mid-flight, reporting that
        nothing had been committed.
        """
        self._recent.clear()
        self._seen.clear()
        self._partial = ""

    def feed(self, text: str) -> bool:
        """Add streamed text. Returns True once the output is a repetition loop."""
        if self.tripped or not text:
            return self.tripped
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            normalized = " ".join(line.split())
            if len(normalized) < self.min_line_chars:
                continue  # short lines ("Good.", "```") are noise, not a cycle
            self._recent.append(hash(normalized))
            if len(self._recent) < self.window:
                continue
            key = tuple(self._recent)
            count = self._seen.get(key, 0) + 1
            if count >= self.threshold:
                self.tripped = True
                return True
            # ponytail: plain dict, cleared wholesale. A response long enough to
            # blow this cap is itself the pathology we're watching for.
            if len(self._seen) > 50_000:
                self._seen.clear()
            self._seen[key] = count
        return False

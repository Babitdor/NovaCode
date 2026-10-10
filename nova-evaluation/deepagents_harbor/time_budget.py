"""Tell an unattended agent what its commands cost and how much time is left.

A timed trial stops the agent without warning, and nothing else in the loop
carries a sense of time: a tool result looks the same whether it took one second
or one minute. Terminal-Bench trials show what that costs. One ran twenty
commands that each piped a 475 MB file through ``od | grep``; at most of a minute
apiece that was its entire 15 minutes, spent before it wrote a line of the
answer. Another spent 163 of its 202 turns studying its input before it first
wrote the script it had been asked for.

So this middleware does three things, all facts about the run:

* a slow call says how long it took;
* the agent gets a short time check as it passes each checkpoint of its budget;
* if a checkpoint arrives and nothing has been written yet, it is told so.
  Being told up front to "write a working version early" changed nothing; being
  told "a third of your time is gone and no file exists" is a different message.

It also keeps a ledger of where the time went — model calls versus each tool —
because the trajectory's own timestamps are all written when it is saved.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Calls quicker than this are not worth a note.
SLOW_SECONDS = 20
# Fractions of the budget at which the agent is told where it stands, once each.
CHECKPOINTS = (0.5, 0.75, 0.9)
# Earlier fractions that speak up ONLY if nothing has been written by then.
NOTHING_WRITTEN_CHECKPOINTS = (0.25, 0.4)

_FILE_TOOLS = frozenset({"write_file", "edit_file"})
_SHELL_TOOLS = frozenset({"bash", "shell", "execute"})
# A shell command that leaves a file behind: a redirect to something other than
# /dev/null or another descriptor, or one of the usual in-place writers.
_SHELL_WRITES = re.compile(
    r"(?<![0-9&>])>>?\s*(?!/dev/null|&)[\w./~\"'$]|\btee\b|\bsed\s+-i|\bpatch\b|\bcp\s|\bmv\s"
)


class TimeBudgetMiddleware(AgentMiddleware):
    """Append call durations and budget checkpoints to tool results; time the run."""

    def __init__(
        self, limit_min: int | None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit_min * 60 if limit_min else None
        self._clock = clock
        self._start = clock()
        self._announced: set[float] = set()
        self._wrote = False
        self._model_seconds = 0.0
        self._model_calls = 0
        self._tool_seconds: dict[str, float] = defaultdict(float)
        self._tool_calls: dict[str, int] = defaultdict(int)

    # ── what the agent is told ───────────────────────────────────────────
    def _saw(self, request: Any) -> str:  # noqa: ANN401
        """Note a tool call; returns the tool's name."""
        call = getattr(request, "tool_call", None) or {}
        name = str(call.get("name") or "")
        if name in _FILE_TOOLS:
            self._wrote = True
        elif name in _SHELL_TOOLS:
            command = str((call.get("args") or {}).get("command") or "")
            if _SHELL_WRITES.search(command):
                self._wrote = True
        return name

    def _note(self, took: float) -> str:
        parts = []
        if took >= SLOW_SECONDS:
            # Rounded to 10s so the same slow command yields the same text each
            # time: the loop guard compares results, and a jittering duration
            # would make an identical repeated call look new.
            parts.append(f"[that call took about {round(took, -1):.0f}s]")
        if self._limit:
            used = self._clock() - self._start
            left = max(0.0, self._limit - used) / 60
            spent = f"about {used / 60:.0f} of {self._limit / 60:.0f} minutes used, about {left:.0f} left"
            early = [
                c
                for c in NOTHING_WRITTEN_CHECKPOINTS
                if used >= c * self._limit and c not in self._announced
            ]
            due = [c for c in CHECKPOINTS if used >= c * self._limit and c not in self._announced]
            self._announced.update(early + due)
            if due:
                parts.append(
                    f"[time check: {spent}. If the required output is not yet at its "
                    f"required path, write a working version there now.]"
                )
            elif early and not self._wrote:
                parts.append(
                    f"[time check: {spent}, and you have not written any file yet. Stop "
                    f"exploring: write a first working version of the required output to "
                    f"its required path now, then test and refine it with the time left.]"
                )
        return "\n\n".join(parts)

    def _annotate(self, result: Any, took: float) -> Any:  # noqa: ANN401
        note = self._note(took)
        if note and isinstance(result, ToolMessage) and isinstance(result.content, str):
            return result.model_copy(update={"content": f"{result.content}\n\n{note}"})
        return result

    # ── hooks ────────────────────────────────────────────────────────────
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Time the call and annotate its result."""
        name = self._saw(request)
        started = self._clock()
        result = handler(request)
        took = self._clock() - started
        self._tool_seconds[name] += took
        self._tool_calls[name] += 1
        return self._annotate(result, took)

    async def awrap_tool_call(  # noqa: ANN401
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """Async twin of :meth:`wrap_tool_call`."""
        name = self._saw(request)
        started = self._clock()
        result = await handler(request)
        took = self._clock() - started
        self._tool_seconds[name] += took
        self._tool_calls[name] += 1
        return self._annotate(result, took)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Time the model call."""
        started = self._clock()
        try:
            return handler(request)
        finally:
            self._model_seconds += self._clock() - started
            self._model_calls += 1

    async def awrap_model_call(  # noqa: ANN401
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """Async twin of :meth:`wrap_model_call`."""
        started = self._clock()
        try:
            return await handler(request)
        finally:
            self._model_seconds += self._clock() - started
            self._model_calls += 1

    # ── the ledger ───────────────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        """Where the wall-clock went, for the trajectory."""
        tools = {
            name: {"calls": self._tool_calls[name], "seconds": round(seconds, 1)}
            for name, seconds in sorted(self._tool_seconds.items(), key=lambda kv: -kv[1])
        }
        return {
            "wall_seconds": round(self._clock() - self._start, 1),
            "model_seconds": round(self._model_seconds, 1),
            "model_calls": self._model_calls,
            "tool_seconds": round(sum(self._tool_seconds.values()), 1),
            "tools": tools,
            "wrote_a_file": self._wrote,
        }

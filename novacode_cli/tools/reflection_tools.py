"""Reflection tools.

This module provides tools for strategic reflection during task execution.
"""

from __future__ import annotations

from langchain.tools import tool


@tool
def think(reflection: str) -> str:
    """Pause and reason about what you just learned before acting on it.

    Use it between tool calls, where it pays off most: after a tool result that
    changes the picture, and before an action that is costly to get wrong. Skip
    it for routine steps. (Models with built-in thinking already reason before
    each response; this is for reasoning over new tool output mid-task.)

    Good moments, with the question to answer:
    - A test or command failed: what does the error actually say, and which
      hypothesis does it rule in or out?
    - Search results came back: which hit is the real definition or call site?
    - About to edit: is this the root cause, or a symptom? What else calls it?
    - A todo is about to be marked done: which check proves it?
    - Two attempts at a step failed: what assumption is wrong?

    Args:
        reflection: The evidence, what it implies, and the decision it leads to.

    Returns:
        Confirmation that the reflection was recorded.
    """
    return f"Reflection recorded: {reflection}"

"""Prove the insert logic places exactly one scorer before the clearing edit.

The full `_build_middleware_stack` needs a real chat model (RubricMiddleware
rejects a placeholder), so this exercises the same insert against a stand-in
list. What is being verified is the *placement rule*, not the whole stack.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")


class ContextEditingMiddleware:
    """Stand-in for the real middleware, matched by class name."""


def apply_insert(stack: list, scorer) -> list:
    """The exact block added to _build_middleware_stack."""
    from novacode_cli.agents.tool_verdicts import (
        build_verdict_middleware,
    )

    if scorer is not None:
        index = next(
            (
                position
                for position, entry in enumerate(stack)
                if type(entry).__name__ == "ContextEditingMiddleware"
            ),
            len(stack),
        )
        stack.insert(index, build_verdict_middleware(scorer))
    return stack


def scorer():
    from novacode_cli.agents.tool_verdicts import (
        FakeDecisionClient,
        ToolVerdictCache,
        VerdictScorer,
    )

    return VerdictScorer(
        FakeDecisionClient(0.1), ToolVerdictCache(path=None), min_interval_seconds=0.0
    )


base = [object(), object(), ContextEditingMiddleware(), object()]

off = apply_insert(list(base), None)
print(f"flag OFF: {len(off)} entries (was {len(base)}) -> unchanged: {len(off) == len(base)}")
print(f"  names: {[type(e).__name__ for e in off]}")

on = apply_insert(list(base), scorer())
names = [type(e).__name__ for e in on]
print(f"flag ON : {len(on)} entries (was {len(base)}) -> one added: {len(on) == len(base) + 1}")
print(f"  names: {names}")
index = names.index("_VerdictMiddleware")
clearing = names.index("ContextEditingMiddleware")
print(f"  verdict middleware at {index}, clearing at {clearing} -> before: {index < clearing}")


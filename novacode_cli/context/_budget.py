"""Context budget tracking across middleware layers.

Private to the ``context`` package. Tracks token usage per middleware layer to
prevent overflow and show which layers consume the most context. Surfaced
through ``ContextManager.budget``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ContextBudget:
    """Track and manage context token usage across middleware layers.

    This helps prevent context overflow by monitoring token usage and
    providing visibility into which middleware layers consume the most context.
    """

    def __init__(self, max_tokens: int = 50000):
        """Initialize context budget tracker.

        Args:
            max_tokens: Maximum allowed tokens before compression/warning.
        """
        self.max_tokens = max_tokens
        self.middleware_usage: dict[str, int] = {}
        self.total_tokens = 0

    def track_middleware(self, middleware_name: str, context: str | list[Any]) -> int:
        """Track context usage for a middleware layer.

        Args:
            middleware_name: Name of the middleware.
            context: Context content (string or list of messages).

        Returns:
            Number of tokens used by this middleware.
        """
        tokens = self._count_tokens(context)
        self.middleware_usage[middleware_name] = tokens
        # Sum, don't accumulate: this is called once per model call with the same
        # (cached) section, so `+=` grew the total forever and logged a bogus
        # "budget exceeded" every turn until the log was nothing else.
        self.total_tokens = sum(self.middleware_usage.values())

        if self.total_tokens > self.max_tokens:
            logger.warning(
                f"Context budget exceeded: {self.total_tokens} > {self.max_tokens} "
                f"tokens. Consider compressing context or reducing middleware layers."
            )

        return tokens

    def _count_tokens(self, context: str | list[Any]) -> int:
        """Estimate token count (~4 chars per token for English text)."""
        if isinstance(context, str):
            return len(context) // 4
        elif isinstance(context, list):
            total = 0
            for msg in context:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        total += len(content) // 4
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                total += len(str(block.get("text", ""))) // 4
                elif isinstance(msg, str):
                    total += len(msg) // 4
            return total
        return 0


    def reset(self) -> None:
        """Reset tracking for a new request."""
        self.middleware_usage.clear()
        self.total_tokens = 0


# Global context budget tracker
_context_budget: ContextBudget | None = None


def get_context_budget(max_tokens: int = 50000) -> ContextBudget:
    """Get or create the global context budget tracker.

    Args:
        max_tokens: Maximum allowed tokens (only used on first call).

    Returns:
        Global ContextBudget instance.
    """
    global _context_budget
    if _context_budget is None:
        _context_budget = ContextBudget(max_tokens)
    return _context_budget


def reset_context_budget() -> None:
    """Reset the global context budget tracker."""
    global _context_budget
    _context_budget = None

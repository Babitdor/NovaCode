"""The one public entry point for context-window management.

``ContextManager`` is a thin, model-aware facade over the package's private
modules (``_analysis``, ``_budget``, ``_model_config``, ``_dynamic``,
``_conversation``). Callers construct it with a model name and call its methods
instead of importing scattered free functions.

    cm = ContextManager(model_name)
    cm.window_size()                       # int
    cm.breakdown(messages, tools=tools)     # ContextBreakdown
    cm.model_config()                      # ModelConfig

Model-independent operations don't require a model name:

    ContextManager().budget()              # ContextBudget (process-global)
    await ContextManager().digest(agent, thread_id)  # str
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novacode_cli.context._analysis import (
    ContextBreakdown,
    build_context_breakdown,
    get_context_window_size,
)
from novacode_cli.context._budget import ContextBudget, get_context_budget
from novacode_cli.context._conversation import get_recent_conversation_digest
from novacode_cli.context._model_config import ModelConfig, get_model_config

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


class ContextManager:
    """Model-aware facade for all context-window operations."""

    def __init__(self, model_name: str | None = None, *, use_dynamic: bool = True):
        """Create a context manager.

        Args:
            model_name: Model to bind analysis/config operations to. May be None
                for model-independent operations (``budget``, ``digest``).
            use_dynamic: Whether to consult Ollama for dynamic window detection.
        """
        self.model_name = model_name
        self.use_dynamic = use_dynamic

    def _require_model(self) -> str:
        if not self.model_name:
            raise ValueError(
                "ContextManager was created without a model_name; this operation "
                "requires one."
            )
        return self.model_name

    # ── Analysis (model-bound) ──────────────────────────────────────────

    def window_size(self) -> int:
        """Context window size in tokens for the bound model."""
        return get_context_window_size(
            self._require_model(), use_dynamic=self.use_dynamic
        )

    def breakdown(
        self, messages: list[BaseMessage], tools: list[Any] | None = None
    ) -> ContextBreakdown:
        """Token breakdown of ``messages`` against the bound model's window.

        Args:
            messages: Conversation messages from agent state.
            tools: Tool definitions bound to the model, if known — their schemas
                are a permanent per-request cost and belong in the baseline.
        """
        return build_context_breakdown(
            messages, self._require_model(), use_dynamic=self.use_dynamic, tools=tools
        )

    def model_config(self) -> ModelConfig:
        """Per-model tuning config (budgets, thresholds, cost) for the bound model."""
        return get_model_config(self._require_model(), use_dynamic=self.use_dynamic)

    # ── Model-independent ───────────────────────────────────────────────

    def budget(self, max_tokens: int = 50000) -> ContextBudget:
        """The process-global middleware context-budget tracker."""
        return get_context_budget(max_tokens)

    async def digest(
        self,
        agent: Any,
        thread_id: str,
        *,
        max_turns: int = 10,
        max_chars: int = 4000,
    ) -> str:
        """Compact transcript of the most recent user/assistant turns."""
        return await get_recent_conversation_digest(
            agent, thread_id, max_turns=max_turns, max_chars=max_chars
        )

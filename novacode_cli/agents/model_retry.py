"""Provider retries with live progress, confined to the current model request."""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware import ModelRetryMiddleware
from langgraph.config import get_stream_writer

from novacode_cli.agents.model_budget import _MODEL_CALL_BUDGET
from novacode_cli.errors.provider_errors import is_context_overflow, is_retryable_model_error


class NovaModelRetryMiddleware(ModelRetryMiddleware):
    """Reuse LangChain's backoff while publishing progress before each wait."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            **{
                "max_retries": 3,
                "retry_on": is_retryable_model_error,
                "on_failure": "error",
                **kwargs,
            }
        )

    @staticmethod
    def _progress(phase: str, attempt: int, maximum: int) -> None:
        try:
            get_stream_writer()({
                "type": "nova_model_retry", "phase": phase,
                "attempt": attempt, "maximum": maximum,
            })
        except RuntimeError:
            # Direct calls outside a LangGraph run have no custom stream.
            pass

    def _report_failure(self, exc: Exception, attempt: int) -> None:
        predicate = self.retry_on
        retryable = (
            isinstance(exc, predicate) if isinstance(predicate, tuple) else predicate(exc)
        )
        if retryable and not is_context_overflow(exc) and attempt <= self.max_retries:
            self._progress("retry", attempt, self.max_retries)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        attempt = 0

        def observed(current: Any) -> Any:
            nonlocal attempt
            attempt += 1
            try:
                result = handler(current)
            except Exception as exc:
                self._report_failure(exc, attempt)
                raise
            if attempt > 1:
                self._progress("recovered", attempt - 1, self.max_retries)
            return result

        return super().wrap_model_call(request, observed)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        attempt = 0

        async def observed(current: Any) -> Any:
            nonlocal attempt
            attempt += 1
            try:
                result = await handler(current)
            except Exception as exc:
                self._report_failure(exc, attempt)
                raise
            if attempt > 1:
                self._progress("recovered", attempt - 1, self.max_retries)
            return result

        return await super().awrap_model_call(request, observed)


class NovaSubagentRetryMiddleware(NovaModelRetryMiddleware):
    """One retry owner and a wall-clock budget per delegated model turn.

    Tool execution and subsequent model turns get their own lifetimes; a slow
    provider request must not hold a local task or teammate indefinitely.
    """

    def __init__(self, *, call_budget: float = _MODEL_CALL_BUDGET, **kwargs: Any) -> None:
        super().__init__(**{"max_retries": 2, **kwargs})
        self.call_budget = call_budget

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        from novacode_cli.agents.model_budget import _bounded_model

        bounded = request.override(model=_bounded_model(request.model))
        return super().wrap_model_call(bounded, handler)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        from novacode_cli.agents.model_budget import _bounded_model

        bounded = request.override(model=_bounded_model(request.model))
        budget = asyncio.timeout(self.call_budget)
        try:
            async with budget:
                return await super().awrap_model_call(bounded, handler)
        except TimeoutError as exc:
            if not budget.expired():
                raise
            raise TimeoutError(
                f"Subagent model request exceeded {self.call_budget:g}s including retries; "
                "the provider may be overloaded. Try another model or retry the task."
            ) from exc

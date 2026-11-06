"""Provider-call accounting over the existing authenticated worker RPC pipe."""

from __future__ import annotations

# Callback signatures follow LangChain's heterogeneous framework interface.
# ruff: noqa: ANN401, ARG002
import json
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from novacode_cli.prime_factory.store import FactoryError


def enforce_output_limit(model: Any, limit: int) -> None:
    """Use the provider's native output bound rather than guessing an unbounded reservation."""
    if type(limit) is not int or limit <= 0:
        message = "Factory output limit must be a positive integer"
        raise FactoryError(message)
    extra = getattr(model, "extra_body", None)
    if isinstance(extra, dict) and "max_tokens" in extra:
        model.extra_body = {**extra, "max_tokens": limit}
        return
    fields = getattr(type(model), "model_fields", {})
    for field in ("max_tokens", "max_output_tokens"):
        if field in fields:
            setattr(model, field, limit)
            return
    message = "Configured factory model cannot enforce an output-token limit"
    raise FactoryError(message)


class FactoryUsageCallback(AsyncCallbackHandler):
    """Cover ordinary, repair and compaction calls on the worker's model object."""

    raise_error = True

    def __init__(self, endpoint: Any, output_limit: int | None = None) -> None:
        """Keep reservations in host storage before provider invocation."""
        self.endpoint = endpoint
        self.reservations: dict[str, str] = {}
        self.output_limit = output_limit

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        """UTF-8 bytes conservatively bound prompt tokens; reserve full output capacity."""
        content = [[message.model_dump(mode="json") for message in batch] for batch in messages]
        await self._start(serialized, content, run_id, kwargs)

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        """Account for completion models used by auxiliary or compaction calls too."""
        await self._start(serialized, prompts, run_id, kwargs)

    async def _start(
        self,
        serialized: dict[str, Any],
        content: object,
        run_id: Any,
        kwargs: dict[str, Any],
    ) -> None:
        if str(run_id) in self.reservations:
            msg = "Duplicate factory model call identity"
            raise FactoryError(msg)
        invocation = kwargs.get("invocation_params", {})
        maximum = invocation.get("max_tokens") or invocation.get("max_completion_tokens")
        if not maximum:
            maximum = (invocation.get("extra_body") or {}).get("max_tokens") or self.output_limit
        if type(maximum) is not int or maximum <= 0:
            message = "Factory model requests require an enforceable output-token bound"
            raise FactoryError(message)
        estimate = (
            len(json.dumps([serialized, content, invocation], default=str).encode()) + maximum
        )
        reply = await self.endpoint("factory_reserve", {"call": str(run_id), "tokens": estimate})
        if not reply.get("reservation"):
            raise FactoryError(reply.get("error", "Factory model capacity unavailable"))
        self.reservations[str(run_id)] = reply["reservation"]

    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Use provider usage when present; missing usage consumes the entire reservation."""
        actual = None
        totals: list[int] = []
        complete = True
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if (
                    isinstance(usage, dict)
                    and type(usage.get("total_tokens")) is int
                    and usage["total_tokens"] >= 0
                ):
                    totals.append(usage["total_tokens"])
                else:
                    complete = False
        if totals and complete:
            actual = sum(totals)
        else:
            # Provider-wide totals cover all generations; partial per-generation
            # metadata cannot justify releasing the remainder of a reservation.
            provider = getattr(response, "llm_output", None) or {}
            usage = provider.get("token_usage") or provider.get("usage") or {}
            if type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
                actual = usage["total_tokens"]
        await self._finish(run_id, actual)

    async def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        """Interrupted calls retain a conservative charge."""
        await self._finish(run_id, None)

    async def _finish(self, run_id: Any, actual: int | None) -> None:
        key = self.reservations.get(str(run_id))
        if key:
            reply = await self.endpoint("factory_reconcile", {"reservation": key, "actual": actual})
            if reply.get("error"):
                raise FactoryError(reply["error"])
            self.reservations.pop(str(run_id), None)

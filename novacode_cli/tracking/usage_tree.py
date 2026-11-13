"""Per-turn LLM usage attribution tree.

Every model in Nova is built through :func:`novacode_cli.config.model_create.build_chat_model`;
a LangChain callback handler attached there records each call's token usage into
the *current usage scope* (a contextvar stack). Loops push scopes around their
work, so a turn's usage forms a tree:

    turn
    ├── main            (base agent stream)
    ├── verification    (inline verifier grading + re-drive attempts)
    │   └── main
    ├── goal            (autonomous re-drives)
    │   └── verification
    │       └── main
    ├── compaction      (/compact + auto-compact summarization)
    └── hermes          (out-of-band self-review)

The tree is created by the shared event loop or session worker, populated by the callback, and
persisted to the durable store at turn end. ``/usage`` renders it.

Design notes:

- ``usage_scope`` is a *sync* context manager — it only manipulates a
  contextvar, so it works across ``await`` points inside async generators.
- Both ``usage_scope`` and the callback are no-ops when no tree is active, so
  tests and non-turn model calls (doctor, model switching) are unaffected.
- Callback attribution is captured at call start, including worker threads
  with propagated context, so concurrent completions keep their owning scope.
"""

from __future__ import annotations

# Callback signatures follow LangChain's heterogeneous framework interface.
# ruff: noqa: ANN401, ARG002
import contextvars
import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("nova.tracking.usage_tree")

#: Durable-store namespace for per-turn usage trees.
_USAGE_NS = ("nova", "usage_trees")
_MAX_REQUEST_RECORDS = 256

#: Per-1M-token USD prices (input, output) for known models — most-specific
#: catalog match wins; unlisted variants have unavailable prices (tokens still shown).
_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gemini-3-pro": (2.0, 12.0),
    "gemini-2.5": (1.25, 10.0),
    "deepseek": (0.27, 1.10),
}


# ═══════════════════════════════════════════════════════════════════════════
# Tree
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class UsageNode:
    """One scope's accumulated usage; children form the attribution tree."""

    scope: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_write_observed_calls: int = 0
    calls: int = 0
    unavailable_calls: int = 0
    cache_observed_input_tokens: int = 0
    cache_observed_read_tokens: int = 0
    model_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    children: list[UsageNode] = field(default_factory=list)
    parent: UsageNode | None = field(default=None, repr=False, compare=False)

    def add(self, model: str, usage: dict[str, int]) -> None:
        """Accumulate one model call's usage into this node."""
        if self.calls and not self.model_usage:
            self.model_usage[self.model] = {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }
        if model:
            self.model = model
        self.input_tokens += int(usage.get("input_tokens", 0))
        self.output_tokens += int(usage.get("output_tokens", 0))
        self.cache_read_tokens += int(usage.get("cache_read_tokens", 0))
        self.cache_creation_tokens += int(usage.get("cache_creation_tokens", 0))
        self.cache_write_observed_calls += int("cache_creation_tokens" in usage)
        if "cache_read_tokens" in usage and "input_tokens" in usage:
            self.cache_observed_input_tokens += int(usage.get("input_tokens", 0))
            self.cache_observed_read_tokens += int(usage["cache_read_tokens"])
        self.calls += 1
        if not usage:
            self.unavailable_calls += 1
        totals = self.model_usage.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        totals["input_tokens"] += int(usage.get("input_tokens", 0))
        totals["output_tokens"] += int(usage.get("output_tokens", 0))

    def cost(self, *, recursive: bool = False) -> float | None:
        """Estimate using each observed model; missing usage or prices stay unknown."""
        if self.unavailable_calls:
            return None
        cost = 0.0
        models = self.model_usage or (
            {
                self.model: {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                }
            }
            if self.calls or self.input_tokens or self.output_tokens
            else {}
        )
        for model, usage in models.items():
            value = estimate_cost(usage["input_tokens"], usage["output_tokens"], model)
            if value is None:
                return None
            cost += value
        if recursive:
            for child in self.children:
                value = child.cost(recursive=True)
                if value is None:
                    return None
                cost += value
        return cost

    def totals(self) -> dict[str, int]:
        """Recursive totals (own + all descendants)."""
        out = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "calls": self.calls,
            "cache_write_observed_calls": self.cache_write_observed_calls,
            "cache_observed_input_tokens": self.cache_observed_input_tokens,
            "cache_observed_read_tokens": self.cache_observed_read_tokens,
        }
        for child in self.children:
            sub = child.totals()
            for key in out:
                out[key] += sub[key]
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serializable form (no parent backrefs)."""
        return {
            "scope": self.scope,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "calls": self.calls,
            "cache_write_observed_calls": self.cache_write_observed_calls,
            "children": [c.to_dict() for c in self.children],
            "unavailable_calls": self.unavailable_calls,
            "cache_observed_input_tokens": self.cache_observed_input_tokens,
            "cache_observed_read_tokens": self.cache_observed_read_tokens,
            "model_usage": {model: dict(usage) for model, usage in self.model_usage.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageNode:
        """Rehydrate a node (and its children) from :meth:`to_dict` output."""
        node = cls(
            scope=data["scope"],
            model=data.get("model", ""),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_creation_tokens=data.get("cache_creation_tokens", 0),
            cache_write_observed_calls=data.get(
                "cache_write_observed_calls", int(data.get("cache_creation_tokens", 0) > 0),
            ),
            calls=data.get("calls", 0),
            unavailable_calls=data.get("unavailable_calls", 0),
            cache_observed_input_tokens=data.get("cache_observed_input_tokens", 0),
            cache_observed_read_tokens=data.get("cache_observed_read_tokens", 0),
            model_usage={key: dict(value) for key, value in data.get("model_usage", {}).items()},
        )
        for child in data.get("children", []):
            node.children.append(cls.from_dict(child))
        return node


class UsageTree:
    """A per-turn usage tree rooted at a ``"turn"`` node."""

    def __init__(self, thread_id: str, started_at: float | None = None) -> None:
        """Create an empty tree for ``thread_id`` (root scope ``"turn"``)."""
        self.thread_id = thread_id
        self.started_at = started_at if started_at is not None else time.time()
        self.record_id = uuid.uuid4().hex
        self.root = UsageNode("turn")
        self._nodes: dict[tuple[str, ...], UsageNode] = {("turn",): self.root}
        self.requests: list[dict[str, Any]] = []
        self.requests_aggregated = 0

    def record_request(self, diagnostic: dict[str, Any]) -> None:
        """Retain at most 256 content-free call records; totals live in nodes."""
        if len(self.requests) < _MAX_REQUEST_RECORDS:
            self.requests.append(diagnostic)
        else:
            self.requests_aggregated += 1

    def node_for(self, path: tuple[str, ...]) -> UsageNode:
        """Get-or-create the node at ``path`` (e.g. ``("verification", "main")``).

        Links parents as needed.
        """
        node = self._nodes.get(path)
        if node is not None:
            return node
        parent = self.node_for(path[:-1]) if len(path) > 1 else self.root
        node = UsageNode(path[-1], parent=parent)
        parent.children.append(node)
        self._nodes[path] = node
        return node

    def record(self, path: tuple[str, ...], model: str, usage: dict[str, int]) -> None:
        """Accumulate one call's usage into the node at ``path``."""
        self.node_for(path).add(model, usage)

    def to_dict(self) -> dict[str, Any]:
        """Serializable form (thread id, start time, root node)."""
        return {
            "thread_id": self.thread_id,
            "started_at": self.started_at,
            "record_id": self.record_id,
            "root": self.root.to_dict(),
            "requests": [dict(record) for record in self.requests],
            "requests_aggregated": self.requests_aggregated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageTree:
        """Rehydrate a tree from :meth:`to_dict` output (rebuilds the index)."""
        tree = cls(data["thread_id"], started_at=data.get("started_at"))
        tree.record_id = data.get("record_id", tree.record_id)
        tree.root = UsageNode.from_dict(data["root"])
        tree.requests = [dict(record) for record in data.get("requests", [])[:_MAX_REQUEST_RECORDS]]
        tree.requests_aggregated = data.get("requests_aggregated", 0)
        # Rebuild the path index from the rehydrated tree.
        tree._nodes = {("turn",): tree.root}

        def _index(node: UsageNode, path: tuple[str, ...]) -> None:
            tree._nodes[path[1:]] = node  # relative path (drop the "turn" root)
            for child in node.children:
                child.parent = node
                _index(child, (*path, child.scope))

        _index(tree.root, ("turn",))
        return tree

    def render(self) -> str:
        """Indented tree with tokens + estimated cost per node."""
        lines: list[str] = []
        total = self.root.totals()
        lines.append(
            f"Turn {time.strftime('%H:%M:%S', time.localtime(self.started_at))} "
            f"(thread {self.thread_id})"
        )

        def _walk(node: UsageNode, depth: int) -> None:
            cost = node.cost()
            cost_text = "unknown" if cost is None else f"${cost:,.2f}"
            indent = "  " * depth
            lines.append(
                f"{indent}{node.scope:<14} "
                f"{node.input_tokens:>9,} in  {node.output_tokens:>7,} out  "
                f"{node.calls:>3} calls  {cost_text}"
            )
            for child in node.children:
                _walk(child, depth + 1)

        for child in self.root.children:
            _walk(child, 1)
        cost = self.root.cost(recursive=True)
        cost_text = "unknown" if cost is None else f"${cost:,.2f}"
        lines.append(
            f"{'total':<14} "
            f"{total['input_tokens']:>9,} in  {total['output_tokens']:>7,} out  "
            f"{total['calls']:>3} calls  {cost_text}"
        )
        writes = (
            f"{total['cache_creation_tokens']:,} written"
            if total["cache_write_observed_calls"] else "writes unavailable"
        )
        if 0 < total["cache_write_observed_calls"] < total["calls"]:
            writes += " (partial reporting)"
        if total["cache_observed_input_tokens"]:
            reuse = total["cache_observed_read_tokens"] / total["cache_observed_input_tokens"] * 100
            lines.insert(
                -1,
                f"Cache reuse: {reuse:.1f}% of observed input tokens "
                f"({total['cache_observed_input_tokens']:,}/{total['input_tokens']:,} observed); "
                f"{total['cache_read_tokens']:,} read, "
                f"{writes}. "
                "Missing cache usage is unavailable. Costs are uncached estimates.",
            )
        elif total["input_tokens"]:
            lines.insert(-1, f"Cache reuse: unavailable; {writes}. Costs are uncached estimates.")
        attributed: dict[tuple[str, ...], dict[str, int]] = {}
        for record in self.requests:
            key = tuple(
                str(record.get(k) or "unknown") for k in ("provider", "model", "purpose", "task_id")
            )
            subtotal = attributed.setdefault(
                key, {"calls": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_read_calls": 0}
            )
            subtotal["calls"] += 1
            subtotal["cache_read_calls"] += int("cache_read_tokens" in record.get("usage", {}))
            for name in ("input_tokens", "cache_read_tokens"):
                subtotal[name] += int(record.get("usage", {}).get(name, 0))
        for (provider, model, purpose, task), subtotal in attributed.items():
            cache_read = (
                f"{subtotal['cache_read_tokens']:,} cache read"
                if subtotal["cache_read_calls"] else "cache read unavailable"
            )
            if 0 < subtotal["cache_read_calls"] < subtotal["calls"]:
                cache_read += " (partial reporting)"
            lines.insert(
                -1,
                f"  {provider}/{model} · {purpose} · task {task}: "
                f"{subtotal['calls']} calls, {subtotal['input_tokens']:,} in, "
                f"{cache_read}",
            )
        if self.requests_aggregated:
            lines.insert(
                -1, f"{self.requests_aggregated} additional requests included in scope totals."
            )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Scope (contextvar stack)
# ═══════════════════════════════════════════════════════════════════════════

_current_tree: contextvars.ContextVar[UsageTree | None] = contextvars.ContextVar(
    "nova_usage_tree", default=None
)
_scope_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "nova_usage_scope", default=()
)


def set_current_tree(tree: UsageTree | None) -> None:
    """Bind the active tree for the current task (turn entry points)."""
    _current_tree.set(tree)


def get_current_tree() -> UsageTree | None:
    """The active tree, or ``None`` outside a turn."""
    return _current_tree.get()


@contextmanager
def usage_scope(name: str) -> Iterator[None]:
    """Push ``name`` onto the scope stack for the block's duration.

    A no-op when no tree is active (tests, non-turn model calls). Nested scopes
    form the tree's parent-child paths.
    """
    if _current_tree.get() is None:
        yield
        return
    token = _scope_stack.set((*_scope_stack.get(), name))
    try:
        yield
    finally:
        _scope_stack.reset(token)


async def scoped_stream(source: AsyncIterator[Any], name: str) -> AsyncIterator[Any]:
    """Attribute stream work without leaving a scope bound across UI yields."""
    path = _scope_stack.get()
    if _current_tree.get() is not None:
        path = (*path, name)
    try:
        while True:
            token = _scope_stack.set(path)
            try:
                item = await anext(source)
            except StopAsyncIteration:
                break
            finally:
                _scope_stack.reset(token)
            yield item
    finally:
        # Closure can run in another task. A fresh token restores that task's
        # scope while keeping model finalization attributed to this stream.
        token = _scope_stack.set(path)
        try:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
        finally:
            _scope_stack.reset(token)


# ═══════════════════════════════════════════════════════════════════════════
# Callback
# ═══════════════════════════════════════════════════════════════════════════


def _extract_usage(response: Any) -> dict[str, int]:
    """Merge normalized and raw usage without conflating absent fields and zero."""
    meta: dict = {}
    raw: dict = {}
    try:
        message = response.generations[0][0].message
        meta = getattr(message, "usage_metadata", None) or {}
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump()
        response_meta = getattr(message, "response_metadata", None) or {}
        raw.update(response_meta.get("token_usage") or response_meta.get("usage") or {})
    except (AttributeError, IndexError, TypeError):
        pass
    output = getattr(response, "llm_output", None) or {}
    raw.update(output.get("token_usage") or output.get("usage") or {})

    def first(*values: Any) -> int | None:
        for value in values:
            if value is not None:
                try:
                    return max(0, int(value))
                except (ValueError, TypeError, OverflowError):
                    continue
        return None

    details = meta.get("input_token_details") or {}
    raw_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    read = first(
        details.get("cache_read"),
        details.get("priority_cache_read"),
        details.get("flex_cache_read"),
        meta.get("cache_read_input_tokens"),
        raw_details.get("cached_tokens"),
        raw.get("cache_read_input_tokens"),
        raw.get("prompt_cache_hit_tokens"),
        raw.get("cachedContentTokenCount"),
        raw.get("cached_content_token_count"),
        raw.get("cacheReadInputTokens"),
    )
    write = first(
        details.get("cache_creation"),
        details.get("priority_cache_creation"),
        details.get("flex_cache_creation"),
        meta.get("cache_creation_input_tokens"),
        raw_details.get("cache_write_tokens"),
        raw.get("cache_creation_input_tokens"),
        raw.get("cacheWriteInputTokens"),
    )
    # LangChain Anthropic sets the generic total to zero when TTL-specific
    # counts are present, to keep its own aggregator from counting them twice.
    tier_writes = sum(
        first(details.get(key)) or 0
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
    )
    if tier_writes:
        write = tier_writes
    input_tokens = first(meta.get("input_tokens"))
    if input_tokens is None:
        input_tokens = first(
            raw.get("prompt_tokens"), raw.get("input_tokens"),
            raw.get("promptTokenCount"), raw.get("prompt_token_count"),
            raw.get("inputTokens"), raw.get("prompt_eval_count"),
        )
        if "inputTokens" in raw and input_tokens is not None:
            input_tokens += (read or 0) + (write or 0)
        # Anthropic raw input excludes cached tokens; normalized input and OpenAI
        # prompt_tokens already include them.
        if (
            input_tokens is not None
            and "prompt_tokens" not in raw
            and ("cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw)
        ):
            input_tokens += (first(raw.get("cache_read_input_tokens")) or 0) + (
                first(raw.get("cache_creation_input_tokens")) or 0
            )
        if (
            input_tokens is None
            and "prompt_cache_hit_tokens" in raw
            and ("prompt_cache_miss_tokens" in raw)
        ):
            input_tokens = (first(raw.get("prompt_cache_hit_tokens")) or 0) + (
                first(raw.get("prompt_cache_miss_tokens")) or 0
            )
    result = {
        "input_tokens": input_tokens,
        "output_tokens": first(
            meta.get("output_tokens"), raw.get("output_tokens"), raw.get("completion_tokens"),
            raw.get("candidatesTokenCount"), raw.get("candidates_token_count"),
            raw.get("outputTokens"), raw.get("eval_count"),
        ),
        "cache_read_tokens": read,
        "cache_creation_tokens": write,
    }
    uncached = first(raw.get("prompt_cache_miss_tokens"))
    if uncached is not None:
        result["uncached_tokens"] = uncached
    elif input_tokens is not None and read is not None and write is not None:
        result["uncached_tokens"] = max(0, input_tokens - read - write)
    return {key: value for key, value in result.items() if value is not None}


def _model_from_serialized(serialized: dict) -> str:
    kwargs = serialized.get("kwargs", {}) or {}
    return str(kwargs.get("model") or kwargs.get("model_name") or serialized.get("name") or "")


class UsageCallbackHandler(BaseCallbackHandler):
    """Attribute calls to their starting task, including concurrent callbacks."""

    def __init__(self) -> None:
        """Keep active call attribution without retaining messages or credentials."""
        super().__init__()
        self._model_names: dict[Any, str] = {}
        self._runs: dict[Any, dict[str, Any]] = {}
        self._lock = RLock()

    def on_chat_model_start(
        self, serialized: dict, messages: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        """Capture chat calls without retaining their messages."""
        self.on_llm_start(serialized, [], run_id=run_id, **kwargs)

    def on_llm_start(self, serialized: dict, prompts: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Capture timing and attribution before context switches."""
        from novacode_cli.tracking.request_metrics import (
            request_diagnostic,
            request_purpose,
            task_correlation,
        )

        metadata = kwargs.get("metadata") or {}
        model = str(
            metadata.get("nova_model")
            or metadata.get("ls_model_name")
            or _model_from_serialized(serialized)
        )
        purpose = request_purpose.get()
        if purpose == "unknown":
            purpose = (_scope_stack.get() or (str(metadata.get("langgraph_node") or "unknown"),))[
                -1
            ]
        with self._lock:
            self._model_names[run_id] = model
            self._runs[run_id] = {
                "tree": _current_tree.get(),
                "path": _scope_stack.get() or ("turn",),
                "started": time.perf_counter(),
                "model": model,
                "provider": str(metadata.get("nova_provider") or metadata.get("ls_provider") or ""),
                "purpose": str(metadata.get("nova_request_purpose") or purpose),
                "task_id": str(metadata.get("nova_task_id") or task_correlation.get() or ""),
                "first_token_ms": None,
                **(request_diagnostic.get() or {}),
            }

    def on_llm_new_token(
        self, token: str | list[str | dict[str, Any]], *, run_id: Any, **kwargs: Any
    ) -> None:
        """Observe first-token time only when the adapter emits a token."""
        with self._lock:
            run = self._runs.get(run_id)
            if token and run is not None and run["first_token_ms"] is None:
                run["first_token_ms"] = (time.perf_counter() - run["started"]) * 1000

    def _finish(self, run_id: Any, usage: dict[str, int], outcome: str) -> None:
        with self._lock:
            self._model_names.pop(run_id, None)
            run = self._runs.pop(run_id, None)
            if run is None:
                return
            tree = run.pop("tree")
            path = run.pop("path")
            run["scope"] = list(path)
            run["duration_ms"] = (time.perf_counter() - run.pop("started")) * 1000
            run["outcome"] = outcome
            run["usage"] = dict(usage)
            if tree is not None:
                tree.record(path, run["model"], usage)
                if usage and ("input_tokens" not in usage or "output_tokens" not in usage):
                    tree.node_for(path).unavailable_calls += 1
                tree.record_request(run)

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Record merged usage and release all in-flight attribution."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None and not run["model"]:
                run["model"] = str(
                    (getattr(response, "llm_output", None) or {}).get("model_name") or ""
                )
        self._finish(run_id, _extract_usage(response), "success")

    def on_llm_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Release failed/cancelled calls while retaining their attribution."""
        self._finish(run_id, {}, "error")


_HANDLER = UsageCallbackHandler()


def attach_usage_callback(model: BaseChatModel) -> None:
    """Append the shared usage handler to a model's default callbacks."""
    existing = list(getattr(model, "callbacks", None) or [])
    if _HANDLER not in existing:
        model.callbacks = [*existing, _HANDLER]


# ═══════════════════════════════════════════════════════════════════════════
# Cost
# ═══════════════════════════════════════════════════════════════════════════


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "") -> float | None:
    """Estimated USD cost using the bundled table; unknown prices return None.

    These are uncached list-price estimates, not a billing ledger. Provider
    discounts, cache prices and unobserved SDK retry usage are unavailable.
    """
    if not model:
        return None
    name = model.casefold().rsplit("/", 1)[-1]
    best_key = ""
    best_price: tuple[float, float] | None = None
    for key, price in _PRICE_PER_M.items():
        # A family substring is not a price for every variant (e.g. Flash vs
        # Pro). Accept only catalog names and their dated snapshot aliases.
        if re.fullmatch(re.escape(key) + r"(?:-\d{4}-?\d{2}-?\d{2})?", name) and len(key) > len(
            best_key
        ):
            best_key = key
            best_price = price
    if best_price is None:
        return None
    return (input_tokens / 1_000_000) * best_price[0] + (output_tokens / 1_000_000) * best_price[1]


# ═══════════════════════════════════════════════════════════════════════════
# Persistence + reporting
# ═══════════════════════════════════════════════════════════════════════════


async def persist_usage_tree(tree: UsageTree, store: Any = None) -> None:
    """Persist a turn's tree to the durable store (best-effort, never raises)."""
    try:
        if store is None:
            from novacode_cli.memory.store import get_durable_store

            store = get_durable_store()
        key = f"{tree.thread_id}:{tree.record_id}"
        await store.aput(_USAGE_NS, key, tree.to_dict())
    except Exception:  # usage tracking must never break a turn
        logger.exception("Failed to persist usage tree")


async def load_usage_trees(thread_id: str, store: Any = None, limit: int = 10) -> list[dict]:
    """Recent usage trees for a thread, newest first (best-effort)."""
    if limit <= 0:
        return []
    try:
        if store is None:
            from novacode_cli.memory.store import get_durable_store

            store = get_durable_store()
        trees: list[dict] = []
        offset = 0
        page_size = 100
        # Stores do not guarantee ordering by the record's started_at. Search
        # this thread in pages, retaining only the newest requested records.
        while True:
            items = await store.asearch(
                _USAGE_NS,
                filter={"thread_id": thread_id},
                limit=page_size,
                offset=offset,
            )
            trees.extend(
                dict(item.value) for item in items if str(item.key).startswith(f"{thread_id}:")
            )
            trees.sort(key=lambda t: t.get("started_at", 0), reverse=True)
            del trees[limit:]
            if len(items) < page_size:
                return trees
            offset += len(items)
    except Exception:  # usage tracking must never break a turn
        logger.exception("Failed to load usage trees")
        return []


def format_usage_report(trees: list[dict]) -> str:
    """Render persisted trees into a human-readable report."""
    if not trees:
        return "No usage data recorded yet for this session."
    return "\n\n".join(UsageTree.from_dict(t).render() for t in trees)

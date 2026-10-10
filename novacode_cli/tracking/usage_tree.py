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

The tree is created by the turn entry points (:func:`execute_task`,
:func:`run_agent_stream`, the session worker), populated by the callback, and
persisted to the durable store at turn end. ``/usage`` renders it.

Design notes:

- ``usage_scope`` is a *sync* context manager — it only manipulates a
  contextvar, so it works across ``await`` points inside async generators.
- Both ``usage_scope`` and the callback are no-ops when no tree is active, so
  tests and non-turn model calls (doctor, model switching) are unaffected.
- Sync model calls in worker threads lose the contextvar and record nothing;
  the async path (the norm) is fully attributed.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("nova.tracking.usage_tree")

#: Durable-store namespace for per-turn usage trees.
_USAGE_NS = ("nova", "usage_trees")

#: Per-1M-token USD prices (input, output) for known models — most-specific
#: substring match wins; unknown/local models cost $0 (tokens still shown).
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
    calls: int = 0
    children: list[UsageNode] = field(default_factory=list)
    parent: UsageNode | None = field(default=None, repr=False, compare=False)

    def add(self, model: str, usage: dict[str, int]) -> None:
        """Accumulate one model call's usage into this node."""
        if model:
            self.model = model
        self.input_tokens += int(usage.get("input_tokens", 0))
        self.output_tokens += int(usage.get("output_tokens", 0))
        self.cache_read_tokens += int(usage.get("cache_read_tokens", 0))
        self.cache_creation_tokens += int(usage.get("cache_creation_tokens", 0))
        self.calls += 1

    def totals(self) -> dict[str, int]:
        """Recursive totals (own + all descendants)."""
        out = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "calls": self.calls,
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
            "children": [c.to_dict() for c in self.children],
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
            calls=data.get("calls", 0),
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
        self.root = UsageNode("turn")
        self._nodes: dict[tuple[str, ...], UsageNode] = {("turn",): self.root}

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
            "root": self.root.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageTree:
        """Rehydrate a tree from :meth:`to_dict` output (rebuilds the index)."""
        tree = cls(data["thread_id"], started_at=data.get("started_at"))
        tree.root = UsageNode.from_dict(data["root"])
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
            cost = estimate_cost(node.input_tokens, node.output_tokens, node.model)
            indent = "  " * depth
            lines.append(
                f"{indent}{node.scope:<14} "
                f"{node.input_tokens:>9,} in  {node.output_tokens:>7,} out  "
                f"{node.calls:>3} calls  ${cost:,.2f}"
            )
            for child in node.children:
                _walk(child, depth + 1)

        for child in self.root.children:
            _walk(child, 1)
        cost = estimate_cost(total["input_tokens"], total["output_tokens"])
        lines.append(
            f"{'total':<14} "
            f"{total['input_tokens']:>9,} in  {total['output_tokens']:>7,} out  "
            f"{total['calls']:>3} calls  ${cost:,.2f}"
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
    """Wrap an async generator so its lifetime runs inside ``usage_scope(name)``.

    The scope is entered on the first pull and exited on exhaustion/close — the
    exact duration of the wrapped model stream.
    """
    with usage_scope(name):
        async for item in source:
            yield item


# ═══════════════════════════════════════════════════════════════════════════
# Callback
# ═══════════════════════════════════════════════════════════════════════════


def _extract_usage(response: Any) -> dict[str, int]:  # noqa: ANN401 — framework object
    """Pull a normalized usage dict from an ``LLMEndResponse``.

    Tries the langchain-normalized ``message.usage_metadata`` first, then the
    provider-specific ``llm_output`` shapes (OpenAI ``token_usage``, Anthropic
    ``usage``).
    """
    try:
        gen = response.generations[0][0]
        meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
        if meta:
            if hasattr(meta, "model_dump"):  # pydantic UsageMetadata, not a dict
                meta = meta.model_dump()
            return {
                "input_tokens": int(meta.get("input_tokens", 0)),
                "output_tokens": int(meta.get("output_tokens", 0)),
                "cache_read_tokens": int(
                    meta.get("cache_read_input_tokens", 0)
                    or (meta.get("input_token_details") or {}).get("cache_read", 0)
                ),
                "cache_creation_tokens": int(
                    meta.get("cache_creation_input_tokens", 0)
                    or (meta.get("input_token_details") or {}).get("cache_creation", 0)
                ),
            }
    except Exception:  # noqa: S110, BLE001 — best-effort extraction
        pass

    llm_output = getattr(response, "llm_output", None) or {}
    usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if not usage:
        return {}
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cache_read_tokens": int(cache_read),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def _model_from_serialized(serialized: dict) -> str:
    kwargs = serialized.get("kwargs", {}) or {}
    return str(kwargs.get("model") or kwargs.get("model_name") or serialized.get("name") or "")


class UsageCallbackHandler(BaseCallbackHandler):
    """Records each LLM call's usage into the current tree + scope.

    Stateless across models (reads contextvars at call time), so one shared
    instance can be attached to every model built by ``build_chat_model``.
    """

    def __init__(self) -> None:
        """Initialize the usage callback handler."""
        super().__init__()
        self._model_names: dict[int, str] = {}

    def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],  # noqa: ARG002
        *,
        run_id: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Capture the model name for a starting LLM run."""
        self._model_names[run_id] = _model_from_serialized(serialized)

    def on_llm_end(
        self,
        response: Any,  # noqa: ANN401
        *,
        run_id: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Record usage for a finished LLM run into the active tree."""
        model = self._model_names.pop(run_id, "") or ""
        tree = _current_tree.get()
        if tree is None:
            return
        usage = _extract_usage(response)
        if not usage:
            return
        if not model:
            llm_output = getattr(response, "llm_output", None) or {}
            model = str(llm_output.get("model_name") or "")
        path = _scope_stack.get() or ("turn",)
        tree.record(path, model, usage)

    def on_llm_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        """Release request metadata on failed or cancelled calls."""
        self._model_names.pop(run_id, None)


_HANDLER = UsageCallbackHandler()


def attach_usage_callback(model: BaseChatModel) -> None:
    """Append the shared usage handler to a model's default callbacks."""
    existing = list(getattr(model, "callbacks", None) or [])
    if _HANDLER not in existing:
        model.callbacks = [*existing, _HANDLER]


# ═══════════════════════════════════════════════════════════════════════════
# Cost
# ═══════════════════════════════════════════════════════════════════════════


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "") -> float:
    """Estimated USD cost; most-specific price-table match, unknown → $0."""
    if not model:
        return 0.0
    best_key = ""
    best_price: tuple[float, float] | None = None
    for key, price in _PRICE_PER_M.items():
        if key in model and len(key) > len(best_key):
            best_key = key
            best_price = price
    if best_price is None:
        return 0.0
    return (input_tokens / 1_000_000) * best_price[0] + (output_tokens / 1_000_000) * best_price[1]


# ═══════════════════════════════════════════════════════════════════════════
# Persistence + reporting
# ═══════════════════════════════════════════════════════════════════════════


async def persist_usage_tree(tree: UsageTree, store: Any = None) -> None:  # noqa: ANN401
    """Persist a turn's tree to the durable store (best-effort, never raises)."""
    try:
        if store is None:
            from novacode_cli.memory.store import get_durable_store

            store = get_durable_store()
        key = f"{tree.thread_id}:{int(tree.started_at)}"
        await store.aput(_USAGE_NS, key, tree.to_dict())
    except Exception:  # usage tracking must never break a turn
        logger.exception("Failed to persist usage tree")


async def load_usage_trees(thread_id: str, store: Any = None, limit: int = 10) -> list[dict]:  # noqa: ANN401
    """Recent usage trees for a thread, newest first (best-effort)."""
    try:
        if store is None:
            from novacode_cli.memory.store import get_durable_store

            store = get_durable_store()
        items = await store.asearch(_USAGE_NS)
        trees = [dict(item.value) for item in items if str(item.key).startswith(f"{thread_id}:")]
        trees.sort(key=lambda t: t.get("started_at", 0), reverse=True)
        return trees[:limit]
    except Exception:  # usage tracking must never break a turn
        logger.exception("Failed to load usage trees")
        return []


def format_usage_report(trees: list[dict]) -> str:
    """Render persisted trees into a human-readable report."""
    if not trees:
        return "No usage data recorded yet for this session."
    return "\n\n".join(UsageTree.from_dict(t).render() for t in trees)

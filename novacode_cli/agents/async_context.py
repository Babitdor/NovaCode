"""Which provider and model an async-agent graph runs on, chosen per dispatch.

The async graphs are built once, when the LangGraph server boots, so their model used
to be fixed for the life of the process: changing it meant editing ``.env`` and
recreating the container. This module makes it a per-run choice instead.

The CLI attaches the stored ``async`` role to every dispatch as the run's ``context``
(:func:`current_async_context`), and :class:`AsyncModelOverrideMiddleware` swaps that
model in for the run. A deployment driven purely by ``docker compose`` still behaves
exactly as before: no context on the wire means the environment wins
(:func:`novacode_cli.config.model_create.build_async_agent_model`).

Precedence, in one place: a stored ``async`` role beats the environment, because it is
the later and more specific statement of intent -- the user picked it in ``/model``.
The one exception is a per-graph variable such as ``PLAN_SCOUT_MODEL``, which is set
with a single agent in mind and therefore still wins over the shared role.

Models are built with :func:`novacode_cli.config.model_create.build_chat_model` rather
than langchain's ``init_chat_model``, because the latter does not know Nova's own
providers (``opencode``, ``nvidia``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

#: Models already built, keyed by ``(provider, model)``. A swap happens on every model
#: call, so building per call would rebuild a client dozens of times per run. A
#: concurrent miss builds twice and the last write wins: one extra object, no harm.
_MODEL_CACHE: dict[tuple[str, str], BaseChatModel] = {}

#: Whether :func:`install_async_model_context` has already wrapped the factories.
_PATCHED = False


@dataclass
class NovaAsyncContext:
    """The run context the CLI attaches to every async dispatch.

    Two plain fields, so the CLI and the graphs can share this without either importing
    the other's machinery.
    """

    provider: str | None = None
    model: str | None = None


def _fields(context: Any) -> tuple[str, str]:
    """Read ``(provider, model)`` off *context*, however it arrived.

    A run context is rehydrated from JSON on the server, so it may be this dataclass or
    a plain mapping depending on how the graph was invoked. Both are accepted.
    """
    if context is None:
        return "", ""
    if isinstance(context, dict):
        provider, model_name = context.get("provider"), context.get("model")
    else:
        provider = getattr(context, "provider", None)
        model_name = getattr(context, "model", None)
    return str(provider or "").strip(), str(model_name or "").strip()


def build_context_model(context: Any) -> BaseChatModel | None:
    """Return the model *context* asks for, or ``None`` to leave the graph alone.

    A context that cannot be built (unknown provider, missing credentials) is logged
    and ignored rather than raised: one bad choice must not fail a background run that
    would otherwise have worked on the graph's own model.
    """
    provider, model_name = _fields(context)
    if not provider or not model_name:
        return None

    key = (provider, model_name)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    from novacode_cli.config.model_create import build_chat_model

    try:
        built = build_chat_model(provider, model_name)
    except Exception:  # noqa: BLE001 - any failure means "use the graph default"
        logger.warning(
            "Async run asked for %s:%s but it could not be built; using the graph default",
            provider,
            model_name,
            exc_info=True,
        )
        return None

    _MODEL_CACHE[key] = built
    return built


class AsyncModelOverrideMiddleware(AgentMiddleware):
    """Run the graph on the run's model, for that run only.

    Outermost on purpose: it has to see every model call before any provider-specific
    middleware handles it.
    """

    def __init__(self, *, per_agent_model_var: str | None = None) -> None:
        """Bind the middleware to one graph.

        Args:
            per_agent_model_var: An environment variable that pins this graph's model,
                such as plan-scout's ``PLAN_SCOUT_MODEL``. When it is set the run
                context is ignored, so a setting made for this one agent still wins over
                the shared ``async`` role.
        """
        super().__init__()
        self._per_agent_model_var = per_agent_model_var

    def _model_for(self, request: Any) -> BaseChatModel | None:
        """The model this request should use, or ``None`` to leave it as it is."""
        var = self._per_agent_model_var
        if var and os.environ.get(var, "").strip():
            return None
        return build_context_model(getattr(request.runtime, "context", None))

    def wrap_model_call(self, request: Any, handler: Any) -> Any:  # noqa: ANN401
        """Route the call to the run's model when it named one."""
        model = self._model_for(request)
        return handler(request if model is None else request.override(model=model))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:  # noqa: ANN401
        """Async twin of :meth:`wrap_model_call`.

        The first build of a model reads Nova's settings: the working directory,
        and each provider key through a lock file (``time.sleep``, ``os.unlink``).
        On the server's event loop those are blocking calls, which ``langgraph
        dev`` refuses outright, so the build failed and every run silently fell
        back to the graph's default model. A cached model is returned inline;
        only a miss goes to a thread.
        """
        key = _fields(getattr(request.runtime, "context", None))
        if not all(key) or key in _MODEL_CACHE:  # nothing to build, or already built
            model = self._model_for(request)
        else:
            model = await asyncio.to_thread(self._model_for, request)
        return await handler(
            request if model is None else request.override(model=model)
        )


def current_async_context() -> NovaAsyncContext | None:
    """The model async agents should run on, as a run context.

    In order: the stored ``async`` role; else, when ``ASYNC_AGENT_PROVIDER`` or
    ``ASYNC_AGENT_MODEL`` is set, ``None`` (the environment is an explicit
    instruction, and the graphs read it themselves); else the main agent's own
    model. That last step is what makes the agents follow whatever provider the
    session runs on: they used to fall back to a fixed Ollama model, a leftover
    of the container they ran in, which failed for anyone without Ollama.
    """
    from novacode_cli.config.nova_config import NovaConfig

    role = NovaConfig().get_role_model("async") or {}
    provider = str(role.get("provider") or "").strip()
    model_name = str(role.get("model") or "").strip()
    if provider and model_name:
        return NovaAsyncContext(provider=provider, model=model_name)

    if (
        os.environ.get("ASYNC_AGENT_PROVIDER", "").strip()
        or os.environ.get("ASYNC_AGENT_MODEL", "").strip()
    ):
        return None
    try:
        from novacode_cli.utils.model_info import get_model_info

        provider, model_name, _display = get_model_info()
    except Exception:  # noqa: BLE001 — no main model to inherit: leave it to the graphs
        return None
    if not provider or not model_name:
        return None
    return NovaAsyncContext(provider=str(provider), model=str(model_name))


def install_async_model_context() -> None:
    """Make every async dispatch carry the stored ``async`` role as run context.

    deepagents builds the dispatch itself -- ``runs.create(thread_id, assistant_id,
    input)`` -- and passes no ``context``, so the remote graph has no way to know which
    model the user picked. Wrapping the client factory is the narrowest seam for that:
    the request is otherwise left exactly as deepagents made it.

    The context is read lazily, at dispatch time, so a ``/model`` change applies to the
    very next launch rather than the next server restart. ``runs.create`` already
    accepts ``context`` in langgraph-sdk and the graphs declare ``context_schema``, so
    this is only the wiring in between.

    Idempotent: calling it twice leaves the factories wrapped once.
    """
    global _PATCHED  # noqa: PLW0603 - the guard is the point
    if _PATCHED:
        return
    _PATCHED = True

    from deepagents.middleware import async_subagents

    for name in ("get_client", "get_sync_client"):
        factory = getattr(async_subagents, name, None)
        if factory is None or getattr(factory, "_nova_context_bound", False):
            continue

        def wrapped(*args: Any, _factory: Any = factory, **kwargs: Any) -> Any:
            """Bind the run context onto the client the factory just built."""
            return _bind_context(_factory(*args, **kwargs))

        wrapped._nova_context_bound = True  # type: ignore[attr-defined]
        setattr(async_subagents, name, wrapped)


def _bind_context(client: Any) -> Any:
    """Return *client* with ``runs.create`` defaulting ``context`` to the async role."""
    # `getattr` with a default widens to `Any | None`, so a client that exposes no
    # `runs` is left alone and the type is narrowed before the monkey-patch below.
    runs: Any = getattr(client, "runs", None)
    if runs is None:
        return client

    original = getattr(runs, "create", None)
    if original is None or getattr(original, "_nova_context_bound", False):
        return client

    def create(*args: Any, **kwargs: Any) -> Any:
        """Create the run, naming the async role's model unless one was given."""
        if kwargs.get("context") is None:
            context = current_async_context()
            if context is not None:
                kwargs["context"] = context
        return original(*args, **kwargs)

    create._nova_context_bound = True  # type: ignore[attr-defined]
    runs.create = create
    return client

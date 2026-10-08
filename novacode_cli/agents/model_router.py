"""Route each turn to one of several configured models, chosen by a decision model.

Nova can already run on any of its providers, but the choice is global and made by
hand in ``/model``. This module makes it per-turn: a System One decision model
(Jev in the cloud, or a local model such as tev1) reads the request and picks one
of the routes the user configured in ``/router``.

Why this is not ``langchain_typesafe.experimental.ModelRouterMiddleware``
-----------------------------------------------------------------------
That middleware takes ``ModelChoice(model="openai:gpt-5-mini")`` -- a *string*
spec resolved by langchain's ``init_chat_model``. That path does not go through
:func:`novacode_cli.config.model_create.build_chat_model`, so it would bypass
every Nova-specific concern: the Ollama content-block patch and ``num_ctx``, the
OpenCode ``name``-stripping subclass, the ``reasoning_effort`` mapping, and the
multimodal profile declaration. A router built on it would silently break local
models and OpenCode -- the two providers the user most wants to route between.

So the *decision* comes from TypeSafe's stable ``TypeSafeClassifier``, and the
*swap* happens here, where ``build_chat_model`` is reachable. That is what makes
the router provider-agnostic by construction rather than by promise.

Failure policy
--------------
Every path fails open to the default route. A router that can break a turn is
worse than no router: the cost of a wrong route is one suboptimal model, and the
cost of a raised exception is a dead session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage

    from novacode_cli.config.nova_config import NovaConfig

logger = logging.getLogger(__name__)

#: Models already built, keyed by ``(provider, model)``. A route is re-resolved on
#: every run, so building per run would rebuild a client each turn. A concurrent
#: miss builds twice and the last write wins: one extra object, no harm.
_MODEL_CACHE: dict[tuple[str, str], BaseChatModel] = {}

#: How many trailing messages are sent to the classifier as state.
#:
#: The decision model is small -- tev1 runs on a context of roughly 2,000 tokens
#: (see ``tool_verdicts``) -- so the whole conversation does not fit. The latest
#: human message is what the routing question is actually about, and a short tail
#: gives it enough context to be read correctly.
DEFAULT_STATE_MESSAGES = 4

#: Characters of a message kept when building the classifier state. A pasted file
#: or a long tool result would otherwise crowd out the question itself.
MAX_MESSAGE_CHARS = 2_000


@dataclass
class RouteDecision:
    """Which route a turn was sent to, and why.

    ``reason`` is set only when the decision did not come from the classifier --
    it is the explanation for a fallback, and is what the TUI shows so a user can
    tell "the model chose this" from "the router gave up and used the default".
    """

    route_id: str | None = None
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    reason: str | None = None

    @property
    def is_fallback(self) -> bool:
        """Whether this decision came from the fallback path rather than the model."""
        return self.reason is not None


def _message_text(message: Any) -> str:
    """Flatten one message's content to text, bounded.

    Content may be a string or a list of blocks (multimodal). Only text blocks
    are kept: the classifier reads natural language, and a base64 image would
    spend the whole context budget on something it cannot use.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "\n".join(parts)
    else:
        text = str(content)
    text = text.strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return text


def build_router_state(
    messages: Sequence[AnyMessage],
    *,
    limit: int = DEFAULT_STATE_MESSAGES,
) -> list[dict[str, str]]:
    """The trailing conversation, as role/content pairs for the classifier.

    Oldest first, so the classifier reads it in the order it happened.
    """
    tail = list(messages)[-limit:] if limit > 0 else []
    state: list[dict[str, str]] = []
    for message in tail:
        text = _message_text(message)
        if not text:
            continue
        role = getattr(message, "type", None) or getattr(message, "role", "user")
        state.append({"role": str(role), "content": text})
    return state


class ModelRouter:
    """Asks a System One decision model which route a request belongs to.

    The client is injectable so tests can drive the decision without a network,
    matching the ``FakeDecisionClient`` pattern already used by ``tool_verdicts``.
    """

    def __init__(
        self,
        config: NovaConfig,
        *,
        client: Any = None,
    ) -> None:
        """Bind the router to a config, and optionally to a decision client."""
        self._config = config
        self._client = client

    def _decision_client(self) -> Any:
        """The System One client, built on first use.

        Built lazily because constructing it reads credentials, and a router that
        is configured but never enabled should not touch the keychain.
        """
        if self._client is None:
            from novacode_cli.agents.openai_decisions import (
                OPENAI_DECISIONS_ENDPOINT,
                OpenAIDecisionsClient,
            )

            if self._config.get_router_decision_endpoint() == OPENAI_DECISIONS_ENDPOINT:
                from novacode_cli.config.credentials import credential_value

                self._client = OpenAIDecisionsClient(
                    model=self._config.get_router_decision_model(),
                    api_key=credential_value("OPENAI_API_KEY"),
                )
                return self._client
            from novacode_cli.agents.tool_verdicts import SystemOneClient

            self._client = SystemOneClient(
                endpoint=self._config.get_router_decision_endpoint(),
                model=self._config.get_router_decision_model(),
                api_key=self._decision_api_key(),
            )
        return self._client

    def _decision_api_key(self) -> str:
        """Resolve the key for the configured endpoint.

        Mirrors ``tool_verdicts.create_system_one_client``: a TypeSafe key is only
        sent to TypeSafe's own endpoint, never to a custom or local server.
        """
        from novacode_cli.config.credentials import credential_value

        endpoint = self._config.get_router_decision_endpoint()
        if endpoint == self._config.TOOL_VERDICT_JEV_ENDPOINT:
            return credential_value("TYPESAFE_API_KEY")
        if endpoint == self._config.TOOL_VERDICT_OPENCODE_ENDPOINT:
            return credential_value("OPENCODE_ZEN_API_KEY")
        return credential_value("SYSTEM_ONE_API_KEY") or "nova"

    def _fallback(self, reason: str) -> RouteDecision:
        """The default route, with the reason the classifier was not used."""
        return RouteDecision(route_id=self._config.get_router_default_route(), reason=reason)

    def decide(self, messages: Sequence[AnyMessage]) -> RouteDecision:
        """Choose a route for *messages*.

        Never raises. Any failure -- no routes, no default, a transport error, a
        malformed response, low confidence -- returns a :class:`RouteDecision`
        carrying the default route and a ``reason``.
        """
        routes = self._config.get_router_routes()
        if not routes:
            return self._fallback("no routes configured")

        criteria = {
            str(route["id"]): str(route["criteria"])
            for route in routes
            if route.get("id") and route.get("criteria")
        }
        if not criteria:
            return self._fallback("no route has criteria")

        state = build_router_state(messages)
        if not state:
            return self._fallback("no message to classify")

        try:
            from novacode_cli.agents.openai_decisions import OpenAIDecisionsClient

            client = self._decision_client()
            if isinstance(client, OpenAIDecisionsClient):
                return self._interpret(client.ask_route({"messages": state}, criteria), criteria)
            # Imported here rather than at module scope so a missing dependency
            # degrades to "routing unavailable" instead of breaking the import of
            # every module that touches the agent.
            from langchain_typesafe import Choice

            question = Choice(
                instructions=(
                    "Which model should handle this request? Choose the "
                    "least costly option that can complete it safely."
                ),
                criteria=criteria,
            )
            response = self._decision_client().ask({"messages": state}, {"route": question})
        except Exception as exc:
            logger.warning("Model routing decision failed; using the default route", exc_info=True)
            return self._fallback(f"classifier failed: {type(exc).__name__}")

        return self._interpret(response, criteria)

    def model_for(self, route_id: str) -> BaseChatModel | None:
        """Build the model a route id names, or ``None`` if it cannot be built.

        Public so the middleware does not have to reach into this object's
        config: the route list is this class's business, and a caller that
        re-reads it would be a second place for the lookup to drift.
        """
        for route in self._config.get_router_routes():
            if str(route.get("id")) == route_id:
                return build_route_model(route)
        return None

    def _interpret(self, response: Any, criteria: dict[str, str]) -> RouteDecision:
        """Turn a System One response into a decision, or fall back.

        The response shape is ``{"answers": {"route": {"choice": ..., "confidence":
        ..., "probabilities": {...}}}}``. Anything else is treated as a failure
        rather than guessed at.
        """
        try:
            answer = response["answers"]["route"]
            choice = str(answer["choice"])
            confidence = float(answer.get("confidence", 0.0))
            probabilities = {
                str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()
            }
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning("Unreadable routing response; using the default route", exc_info=True)
            return self._fallback(f"unreadable response: {type(exc).__name__}")

        import math

        if (
            not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or any(
                not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values()
            )
        ):
            return self._fallback("invalid classifier probability")
        if choice not in criteria:
            return self._fallback(f"classifier chose unknown route {choice!r}")

        threshold = self._config.get_router_min_confidence()
        if confidence < threshold:
            return RouteDecision(
                route_id=self._config.get_router_default_route(),
                confidence=confidence,
                probabilities=probabilities,
                reason=f"confidence {confidence:.2f} below {threshold:.2f}",
            )

        return RouteDecision(
            route_id=choice,
            confidence=confidence,
            probabilities=probabilities,
        )


def build_route_model(route: dict[str, Any]) -> BaseChatModel | None:
    """Build the model a route names, through Nova's own constructor.

    Returns ``None`` when the route cannot be built, so the caller can leave the
    run on its existing model rather than failing it.

    This is the function that makes the router provider-agnostic: every route
    goes through :func:`build_chat_model`, so an Ollama route keeps its
    ``num_ctx`` and content-block patch, and an OpenCode route keeps its
    ``name``-stripping subclass.
    """
    provider = str(route.get("provider", "")).strip()
    model_name = str(route.get("model", "")).strip()
    if not provider or not model_name:
        return None

    key = (provider, model_name)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    from novacode_cli.config.model_create import build_chat_model

    try:
        built = build_chat_model(provider, model_name)
    except Exception:
        logger.warning(
            "Route %s:%s could not be built; leaving the run on its current model",
            provider,
            model_name,
            exc_info=True,
        )
        return None

    _MODEL_CACHE[key] = built
    return built


class RouterState(AgentState):
    """Persist the selected route in the compiled graph's checkpoint schema."""

    model_route: str | None


class ModelRouterMiddleware(AgentMiddleware):
    """Run each turn on the model the router chose for it.

    The decision is made once per run, in ``before_agent``, and the swap happens
    in ``wrap_model_call``. Splitting it that way means the classifier is asked
    once per turn rather than once per model call -- a tool-using turn makes many
    model calls, and re-asking each time would multiply the decision-model load
    for no benefit, since the request has not changed.
    """

    state_schema = RouterState

    def __init__(self, router: ModelRouter) -> None:
        """Bind the middleware to a router."""
        super().__init__()
        self._router = router
        #: Compatibility for direct calls without a ModelRequest state. Compiled
        #: runs read their checkpoint route, so simultaneous threads stay isolated.
        self._current: RouteDecision | None = None

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Classify the run's request and record the chosen route in state."""
        messages = state.get("messages", []) if isinstance(state, dict) else []
        decision = self._router.decide(messages)
        self._current = decision
        return {"model_route": decision.route_id}

    def _model_for_current_route(self) -> BaseChatModel | None:
        """The model for the route chosen this run, or ``None`` to leave it alone."""
        decision = self._current
        if decision is None or decision.route_id is None:
            return None
        return self._router.model_for(decision.route_id)

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Keep decision HTTP requests and credential reads off the event loop."""
        import asyncio

        return await asyncio.to_thread(self.before_agent, state, runtime)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        """Run the call on the routed model when one was chosen."""
        state = getattr(request, "state", None)
        model = (
            (self._router.model_for(state["model_route"]) if state.get("model_route") else None)
            if isinstance(state, dict)
            else self._model_for_current_route()
        )
        return handler(request if model is None else request.override(model=model))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Async twin of :meth:`wrap_model_call`.

        The first build of a model reads Nova's settings and provider keys, which
        are blocking calls; on an event loop that is worth moving to a thread. A
        cached model is returned inline.
        """
        import asyncio

        state = getattr(request, "state", None)
        if isinstance(state, dict):
            route_id = state.get("model_route")
            model = await asyncio.to_thread(self._router.model_for, route_id) if route_id else None
        else:
            model = await asyncio.to_thread(self._model_for_current_route)
        return await handler(request if model is None else request.override(model=model))

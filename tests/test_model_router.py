"""Model routing: config, decision, and the middleware that enforces it.

The behaviour worth pinning here is that routing is *off* unless asked for, and
that when it is on it builds its models through Nova's own constructor. The
second one is the whole reason this feature is not built on TypeSafe's
``ModelRouterMiddleware``: that middleware resolves ``"openai:gpt-5-mini"``
through langchain's ``init_chat_model``, which does not know Nova's providers and
would drop the Ollama ``num_ctx`` and the OpenCode subclass. A test that only
asserted "a model was returned" would pass against that broken version, so the
assertions below check the *type* and the provider-specific attributes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.model_router import (
    ModelRouter,
    ModelRouterMiddleware,
    RouteDecision,
    build_route_model,
    build_router_state,
)
from novacode_cli.config.nova_config import NovaConfig


@pytest.fixture(autouse=True)
def tmp_config(monkeypatch, tmp_path):
    """Point NovaConfig at a throwaway directory, and assert that it did.

    Same guard as ``test_role_models``: without the assertion a green suite cannot
    tell real isolation from isolation that quietly stopped working, and this
    suite writes routes.
    """
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.Settings.from_environment",
        lambda: SimpleNamespace(user_deepagents_dir=tmp_path / "nova"),
        raising=True,
    )
    cfg = NovaConfig()
    assert str(cfg.config_path).startswith(str(tmp_path)), (
        f"isolation failed: config_path is {cfg.config_path}, not under {tmp_path}"
    )
    return tmp_path


class FakeDecisionClient:
    """A System One client that answers from a fixed payload."""

    def __init__(self, response=None, *, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self._raises is not None:
            raise self._raises
        return self._response


def _answer(choice: str, confidence: float = 0.9, probabilities=None) -> dict:
    return {
        "answers": {
            "route": {
                "choice": choice,
                "confidence": confidence,
                "probabilities": probabilities or {choice: confidence},
            }
        }
    }


def _configured(**overrides) -> NovaConfig:
    """A config with two routes and a default, ready to route."""
    cfg = NovaConfig()
    cfg.set_router_routes(
        overrides.pop(
            "routes",
            [
                {"id": "fast", "provider": "ollama", "model": "m", "criteria": "Simple work."},
                {"id": "deep", "provider": "openai", "model": "g", "criteria": "Hard work."},
            ],
        )
    )
    cfg.set_router_default_route(overrides.pop("default_route", "fast"))
    for key, value in overrides.items():
        getattr(cfg, f"set_router_{key}")(value)
    return cfg


# ── config ──────────────────────────────────────────────────────────────────


def test_routing_is_off_by_default():
    """The feature must not change behaviour for anyone who never enables it."""
    cfg = NovaConfig()
    assert cfg.get_router_enabled() is False
    assert cfg.get_router_routes() == []
    assert cfg.get_router_default_route() is None


def test_decision_defaults_match_the_tool_verdict_endpoint():
    """A user who already configured Jev or tev1 gets routing working for free."""
    cfg = NovaConfig()
    assert cfg.get_router_decision_endpoint() == cfg.TOOL_VERDICT_DEFAULT_ENDPOINT
    assert cfg.get_router_decision_model() == cfg.TOOL_VERDICT_DEFAULT_MODEL


def test_routes_round_trip_through_disk():
    cfg = _configured()
    reloaded = NovaConfig()
    assert reloaded.get_router_routes() == cfg.get_router_routes()
    assert reloaded.get_router_default_route() == "fast"


def test_router_profiles_keep_independent_routes_and_active_selection():
    cfg = _configured()
    cfg.set_active_router_profile(cfg.create_router_profile("General"))
    cfg.set_router_routes(
        [{"id": "chat", "provider": "openai", "model": "gpt", "criteria": "General chat."}]
    )
    cfg.set_router_enabled(True)

    reloaded = NovaConfig()
    assert reloaded.get_active_router_profile() == "general"
    assert [p["name"] for p in reloaded.get_router_profiles()] == ["Default", "General"]
    assert [r["id"] for r in reloaded.get_router_routes()] == ["chat"]
    reloaded.set_active_router_profile("default")
    assert [r["id"] for r in reloaded.get_router_routes()] == ["fast", "deep"]
    assert reloaded.get_router_enabled() is False


def test_legacy_router_settings_are_migrated_without_losing_routes():
    cfg = _configured()
    assert cfg.get_router_profiles() == [{"id": "default", "name": "Default"}]
    assert cfg.get_router_routes()[0]["id"] == "fast"
    cfg.set_router_enabled(True)
    reloaded = NovaConfig()
    assert reloaded.get_active_router_profile() == "default"
    assert [r["id"] for r in reloaded.get_router_routes()] == ["fast", "deep"]
    assert reloaded.get_router_enabled() is True


def test_cannot_delete_last_router_profile():
    cfg = NovaConfig()
    with pytest.raises(ValueError, match="At least one router profile"):
        cfg.delete_router_profile("default")


def test_get_routes_returns_a_copy():
    """The TUI edits a working list; an abandoned edit must not reach the config."""
    cfg = _configured()
    routes = cfg.get_router_routes()
    routes[0]["id"] = "mutated"
    routes.append({"id": "extra"})
    assert cfg.get_router_routes()[0]["id"] == "fast"
    assert len(cfg.get_router_routes()) == 2


@pytest.mark.parametrize(
    ("routes", "match"),
    [
        ([{"id": "a", "provider": "nope", "model": "m", "criteria": "c"}], "unknown provider"),
        (
            [
                {"id": "a", "provider": "ollama", "model": "m", "criteria": "c"},
                {"id": "a", "provider": "ollama", "model": "m", "criteria": "c"},
            ],
            "Duplicate route id",
        ),
        ([{"id": "a", "provider": "ollama", "model": "", "criteria": "c"}], "no model"),
        ([{"id": "a", "provider": "ollama", "model": "m", "criteria": ""}], "no criteria"),
        ([{"id": "", "provider": "ollama", "model": "m", "criteria": "c"}], "no id"),
    ],
)
def test_invalid_routes_are_rejected(routes, match):
    """Validation lives at the write, so a bad route never reaches model construction."""
    cfg = NovaConfig()
    with pytest.raises(ValueError, match=match):
        cfg.set_router_routes(routes)


def test_enabled_flag_survives_a_hand_edited_string():
    """``bool("false")`` is True, so the flag is parsed strictly."""
    cfg = NovaConfig()
    cfg._config["router"] = {"enabled": "false"}
    assert cfg.get_router_enabled() is False


# ── decision ────────────────────────────────────────────────────────────────


def test_confident_choice_is_taken():
    cfg = _configured()
    router = ModelRouter(cfg, client=FakeDecisionClient(_answer("deep", 0.91)))
    decision = router.decide([HumanMessage("Prove the primes are infinite.")])
    assert decision.route_id == "deep"
    assert decision.confidence == pytest.approx(0.91)
    assert decision.is_fallback is False


def test_low_confidence_falls_back_to_the_default():
    """A coin-flip between two models is worse than a predictable one."""
    cfg = _configured()
    router = ModelRouter(cfg, client=FakeDecisionClient(_answer("deep", 0.3)))
    decision = router.decide([HumanMessage("Something ambiguous.")])
    assert decision.route_id == "fast"
    assert decision.is_fallback is True
    assert "below" in decision.reason


def test_a_failing_classifier_never_raises():
    """The router must not be able to break a turn."""
    cfg = _configured()
    router = ModelRouter(cfg, client=FakeDecisionClient(raises=RuntimeError("network down")))
    decision = router.decide([HumanMessage("Anything.")])
    assert decision.route_id == "fast"
    assert "RuntimeError" in decision.reason


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"answers": {}},
        {"answers": {"route": {}}},
        {"answers": {"route": {"choice": "deep"}}},
        {"answers": {"route": {"choice": "deep", "confidence": "not-a-number"}}},
    ],
)
def test_unreadable_responses_fall_back(response):
    cfg = _configured()
    router = ModelRouter(cfg, client=FakeDecisionClient(response))
    decision = router.decide([HumanMessage("Anything.")])
    assert decision.route_id == "fast"
    assert decision.is_fallback is True


def test_unknown_route_from_the_classifier_falls_back():
    """A hallucinated route id must not be trusted into a model build."""
    cfg = _configured()
    router = ModelRouter(cfg, client=FakeDecisionClient(_answer("ghost")))
    decision = router.decide([HumanMessage("Anything.")])
    assert decision.route_id == "fast"
    assert "unknown route" in decision.reason


def test_no_routes_means_no_decision():
    cfg = NovaConfig()
    router = ModelRouter(cfg, client=FakeDecisionClient(_answer("x")))
    decision = router.decide([HumanMessage("Anything.")])
    assert decision.route_id is None
    assert "no routes" in decision.reason


def test_the_classifier_is_asked_once_with_every_route():
    cfg = _configured()
    client = FakeDecisionClient(_answer("fast"))
    ModelRouter(cfg, client=client).decide([HumanMessage("Hello.")])
    assert len(client.calls) == 1
    question = client.calls[0]["questions"]["route"]
    criteria = question.criteria
    assert set(criteria) == {"fast", "deep"}


# ── state building ──────────────────────────────────────────────────────────


def test_state_is_bounded_to_the_tail():
    """The decision model's context is small; the whole conversation does not fit."""
    messages = [HumanMessage(f"m{i}") for i in range(20)]
    state = build_router_state(messages, limit=3)
    assert [m["content"] for m in state] == ["m17", "m18", "m19"]


def test_state_keeps_only_text_blocks():
    """A base64 image would spend the whole context budget on something unusable."""
    message = HumanMessage(
        content=[
            {"type": "text", "text": "What is this?"},
            {"type": "image", "base64": "A" * 5000},
        ]
    )
    state = build_router_state([message])
    assert state[0]["content"] == "What is this?"


def test_state_truncates_a_huge_message():
    state = build_router_state([HumanMessage("x" * 10_000)])
    assert len(state[0]["content"]) < 2_100


def test_state_skips_empty_messages():
    state = build_router_state([HumanMessage(""), AIMessage("   "), HumanMessage("real")])
    assert [m["content"] for m in state] == ["real"]


def test_state_carries_roles():
    state = build_router_state(
        [HumanMessage("hi"), AIMessage("hello"), ToolMessage("r", tool_call_id="1")]
    )
    assert [m["role"] for m in state] == ["human", "ai", "tool"]


# ── model construction ──────────────────────────────────────────────────────


def test_route_model_goes_through_novas_constructor():
    """The regression that would silently break local models.

    ``build_chat_model`` gives Ollama its ``num_ctx``. A router built on
    langchain's ``init_chat_model`` would return a ChatOllama without it, and
    this assertion is what catches that.
    """
    model = build_route_model({"provider": "ollama", "model": "qwen3-vl:235b-cloud"})
    assert model is not None
    assert type(model).__name__ == "ChatOllama"
    assert getattr(model, "num_ctx", None) is not None


def test_route_models_are_cached():
    """A route is re-resolved every run; rebuilding a client each turn is waste."""
    first = build_route_model({"provider": "ollama", "model": "cache-probe"})
    second = build_route_model({"provider": "ollama", "model": "cache-probe"})
    assert first is second


def test_an_unbuildable_route_returns_none_rather_than_raising(monkeypatch):
    """A route that cannot be built leaves the run on its current model."""

    def unavailable(*args, **kwargs):
        raise ValueError("Provider unavailable")

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", unavailable)
    assert build_route_model({"provider": "openai", "model": "unavailable-test"}) is None
    assert build_route_model({"provider": "", "model": ""}) is None


# ── middleware ──────────────────────────────────────────────────────────────


def test_middleware_records_the_route_in_state():
    cfg = _configured()
    middleware = ModelRouterMiddleware(ModelRouter(cfg, client=FakeDecisionClient(_answer("deep"))))
    update = middleware.before_agent({"messages": [HumanMessage("Hard problem.")]}, None)
    assert update == {"model_route": "deep"}


def test_middleware_swaps_in_the_routed_model():
    cfg = _configured(
        routes=[{"id": "local", "provider": "ollama", "model": "m", "criteria": "Local."}]
    )
    middleware = ModelRouterMiddleware(
        ModelRouter(cfg, client=FakeDecisionClient(_answer("local")))
    )
    middleware.before_agent({"messages": [HumanMessage("Do it locally.")]}, None)
    model = middleware._model_for_current_route()
    assert type(model).__name__ == "ChatOllama"


def test_middleware_leaves_the_model_alone_when_the_route_cannot_be_built(monkeypatch):
    """Fail-open: an unbuildable route must not fail the run."""

    def unavailable(*args, **kwargs):
        raise ValueError("Provider unavailable")

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", unavailable)
    cfg = _configured(
        routes=[
            {"id": "cloud", "provider": "openai", "model": "unavailable-test", "criteria": "Cloud."}
        ]
    )
    middleware = ModelRouterMiddleware(
        ModelRouter(cfg, client=FakeDecisionClient(_answer("cloud")))
    )
    middleware.before_agent({"messages": [HumanMessage("Do it in the cloud.")]}, None)
    assert middleware._model_for_current_route() is None


def test_middleware_does_nothing_before_a_decision():
    """No decision yet means no override, not a crash."""
    cfg = _configured()
    middleware = ModelRouterMiddleware(ModelRouter(cfg, client=FakeDecisionClient(_answer("fast"))))
    assert middleware._model_for_current_route() is None


def test_wrap_model_call_overrides_only_when_a_model_was_chosen():
    """The override is the mechanism; a None model must pass the request through."""
    cfg = _configured(
        routes=[{"id": "local", "provider": "ollama", "model": "m", "criteria": "Local."}]
    )
    middleware = ModelRouterMiddleware(
        ModelRouter(cfg, client=FakeDecisionClient(_answer("local")))
    )

    seen: list = []

    class Request:
        def override(self, **kwargs):
            seen.append(kwargs)
            return "overridden"

    # No decision yet: the request is handed through untouched.
    request = Request()
    assert middleware.wrap_model_call(request, lambda r: r) is request
    assert seen == []

    middleware.before_agent({"messages": [HumanMessage("Local please.")]}, None)
    result = middleware.wrap_model_call(Request(), lambda r: r)
    assert result == "overridden"
    assert len(seen) == 1
    assert type(seen[0]["model"]).__name__ == "ChatOllama"


@pytest.mark.asyncio
async def test_compiled_router_routes_each_turn_and_persists_selection(monkeypatch):
    """Exercise the real LangChain graph, not just middleware methods."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver

    models = {
        "fast": FakeMessagesListChatModel(responses=[AIMessage(content="fast answer")]),
        "deep": FakeMessagesListChatModel(responses=[AIMessage(content="deep answer")]),
    }
    cfg = _configured()
    client = FakeDecisionClient(_answer("deep"))
    router = ModelRouter(cfg, client=client)
    monkeypatch.setattr(router, "model_for", lambda route_id: models.get(route_id))
    graph = create_agent(
        model=models["fast"],
        middleware=[ModelRouterMiddleware(router)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "route-test"}}
    first = await graph.ainvoke({"messages": [HumanMessage("Difficult code problem")]}, config)
    assert first["messages"][-1].content == "deep answer"
    assert (await graph.aget_state(config)).values["model_route"] == "deep"
    client._response = _answer("fast")
    second = await graph.ainvoke({"messages": [HumanMessage("Simple follow-up")]}, config)
    assert second["messages"][-1].content == "fast answer"
    assert second["model_route"] == "fast"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_router_uses_each_requests_route_in_concurrent_runs(monkeypatch):
    import asyncio

    router = ModelRouter(_configured(), client=FakeDecisionClient(_answer("deep")))
    monkeypatch.setattr(router, "model_for", lambda route_id: route_id)
    middleware = ModelRouterMiddleware(router)
    middleware.before_agent({"messages": [HumanMessage("Other run")]}, None)

    class Request:
        def __init__(self, route):
            self.state = {"model_route": route}

        def override(self, **kwargs):
            return kwargs["model"]

    async def handler(request):
        return request

    assert await asyncio.gather(
        middleware.awrap_model_call(Request("fast"), handler),
        middleware.awrap_model_call(Request("deep"), handler),
    ) == ["fast", "deep"]


@pytest.mark.asyncio
async def test_compiled_router_keeps_route_through_tool_loop(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def read_example() -> str:
        """Read a small example."""
        return "example contents"

    routed = ToolModel(
        responses=[
            AIMessage("", tool_calls=[{"name": "read_example", "args": {}, "id": "call-1"}]),
            AIMessage("routed final answer"),
        ]
    )
    base = ToolModel(responses=[AIMessage("wrong model")])
    client = FakeDecisionClient(_answer("deep"))
    router = ModelRouter(_configured(), client=client)
    monkeypatch.setattr(router, "model_for", lambda route_id: routed)
    graph = create_agent(
        model=base, tools=[read_example], middleware=[ModelRouterMiddleware(router)]
    )
    result = await graph.ainvoke({"messages": [HumanMessage("Read example and explain")]})
    assert result["messages"][-1].content == "routed final answer"
    assert any(
        isinstance(msg, ToolMessage) and msg.content == "example contents"
        for msg in result["messages"]
    )
    assert result["model_route"] == "deep"
    assert len(client.calls) == 1


# ── agent wiring ────────────────────────────────────────────────────────────


def test_router_middleware_is_absent_when_disabled():
    """Off by default means absent from the stack, not present and inert."""
    from novacode_cli.agents.core_agent import _build_router_middleware

    assert _build_router_middleware() is None


def test_router_middleware_is_absent_with_routes_but_the_flag_off():
    from novacode_cli.agents.core_agent import _build_router_middleware

    cfg = NovaConfig()
    cfg.set_router_routes([{"id": "a", "provider": "ollama", "model": "m", "criteria": "c"}])
    assert _build_router_middleware() is None


def test_router_middleware_is_built_when_enabled_with_routes():
    from novacode_cli.agents.core_agent import _build_router_middleware

    cfg = _configured()
    cfg.set_router_enabled(True)
    assert isinstance(_build_router_middleware(), ModelRouterMiddleware)


def test_route_decision_reports_fallback_state():
    """``is_fallback`` is what lets the TUI say why a route was chosen."""
    assert RouteDecision(route_id="a").is_fallback is False
    assert RouteDecision(route_id="a", reason="because").is_fallback is True


# ── the /router screen ──────────────────────────────────────────────────────


def test_router_screen_refuses_a_route_with_no_provider():
    """An unset Select holds ``Select.NULL``, not ``Select.BLANK``.

    Both mean "nothing chosen", and a check against only one of them lets a
    provider-less route through to ``set_router_routes``, which then rejects it
    with a message about an unknown provider -- far from the real cause.
    """
    import asyncio

    from textual.app import App
    from textual.widgets import Input, Select

    from novacode_cli.tui.screens import RouterScreen

    class Harness(App):
        def on_mount(self) -> None:
            self.push_screen(RouterScreen(NovaConfig()))

    async def run() -> tuple[bool, bool]:
        app = Harness()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one("#router-id", Input).value = "x"
            screen.query_one("#router-model", Input).value = "m"
            screen.query_one("#router-criteria", Input).value = "c"
            refused = screen._read_fields() is None
            screen.query_one("#router-provider", Select).value = "ollama"
            accepted = screen._read_fields() is not None
        return refused, accepted

    refused, accepted = asyncio.run(run())
    assert refused, "a route with no provider must be refused"
    assert accepted, "a complete route must be accepted"


def test_router_screen_add_route_appends_even_when_existing_route_is_selected():
    """Add must support several route IDs; editing uses its own explicit action."""
    import asyncio

    from textual.app import App
    from textual.widgets import Input, Select

    from novacode_cli.tui.screens import RouterScreen

    class Harness(App):
        def on_mount(self) -> None:
            cfg = NovaConfig()
            cfg.set_router_routes(
                [{"id": "quick", "provider": "ollama", "model": "q", "criteria": "Simple."}]
            )
            self.push_screen(RouterScreen(cfg))

    async def run() -> list[str]:
        app = Harness()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one("#router-id", Input).value = "coding"
            screen.query_one("#router-model", Input).value = "coder"
            screen.query_one("#router-criteria", Input).value = "Code changes."
            screen.query_one("#router-provider", Select).value = "openai"
            screen._add_route()
            return [route["id"] for route in screen._routes]

    assert asyncio.run(run()) == ["quick", "coding"]

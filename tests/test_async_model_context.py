"""Per-run model selection for the async-agent graphs.

The async graphs are compiled when the LangGraph server boots, so their model used to
be fixed until the container was recreated. These tests pin the replacement: a dispatch
carries the stored ``async`` role as the run's context, the graph swaps that model in for
the run, and a deployment that sends no context keeps running on the environment.

Two of them are worth reading if you are here because they went red:

- ``test_the_graph_actually_runs_on_the_context_model`` asserts on what the run used, not
  on what the middleware says. It fails if the override is wired but ignored.
- ``test_the_dispatch_contract_holds`` is the canary for a deepagents bump: it fails if
  the client factories move or ``runs.create`` stops accepting ``context``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.agents import async_context as ac
from novacode_cli.agents.async_context import (
    AsyncModelOverrideMiddleware,
    NovaAsyncContext,
    build_context_model,
    current_async_context,
)


class _Named(GenericFakeChatModel):
    """A fake model that echoes the id it was given, so a swap is observable."""

    def __init__(self, name: str) -> None:
        super().__init__(messages=iter([AIMessage(content=f"ran-on:{name}")]))

    @property
    def _llm_type(self) -> str:
        return "named-fake"

    def bind_tools(self, tools: object, **kwargs: object) -> _Named:  # noqa: ARG002
        """The agent binds tools before each call; a fake only needs to accept it."""
        return self


@dataclass
class _Recorder(AgentMiddleware):
    """Records the model each call was made on, then delegates."""

    seen: list[str]

    def wrap_model_call(self, request, handler):  # noqa: ANN001
        self.seen.append(request.model.model_name if hasattr(request.model, "model_name") else "")
        return handler(request)


# ── the model builder ────────────────────────────────────────────────────────


def test_no_context_leaves_the_graph_alone():
    """An empty context is how "the environment decides" is expressed."""
    assert build_context_model(None) is None
    assert build_context_model(NovaAsyncContext()) is None
    assert build_context_model(NovaAsyncContext(provider="opencode")) is None
    assert build_context_model(NovaAsyncContext(model="gpt-5")) is None


def test_a_context_model_is_built_through_novas_own_builder(monkeypatch, fake_chat_model):
    """Nova's providers are not langchain's, so `build_chat_model` must be the route.

    `init_chat_model` cannot resolve `opencode`, which is what broke the CLI earlier:
    this asserts the context path does not repeat that mistake.
    """
    calls: list[tuple[str, str]] = []

    def fake_build(provider: str, model_name: str):
        calls.append((provider, model_name))
        return fake_chat_model

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", fake_build)
    ac._MODEL_CACHE.clear()

    built = build_context_model(NovaAsyncContext(provider="opencode", model="deepseek-v4.1-flash"))

    assert built is fake_chat_model
    assert calls == [("opencode", "deepseek-v4.1-flash")]


def test_a_context_model_is_built_once_not_per_call(monkeypatch, fake_chat_model):
    """A swap happens on every model call, so a per-call build would be waste."""
    calls: list[int] = []

    def fake_build(provider: str, model_name: str) -> _Named:  # noqa: ARG001
        calls.append(1)
        return fake_chat_model

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", fake_build)
    ac._MODEL_CACHE.clear()

    context = NovaAsyncContext(provider="ollama", model="tev1:4b")
    for _ in range(5):
        build_context_model(context)

    assert len(calls) == 1


def test_a_context_model_that_cannot_be_built_falls_back(monkeypatch):
    """One bad choice must not fail a background run that would otherwise work."""
    from novacode_cli.config.model_create import build_chat_model as real_build

    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, model: (_ for _ in ()).throw(ValueError("Unknown provider")),
    )
    ac._MODEL_CACHE.clear()

    assert build_context_model(NovaAsyncContext(provider="nope", model="x")) is None
    # And the real builder is still importable/untouched for the caller.
    assert callable(real_build)


# ── the middleware ───────────────────────────────────────────────────────────


def test_the_middleware_swaps_the_model_when_the_run_named_one(monkeypatch, fake_chat_model):
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, model: fake_chat_model,
    )
    ac._MODEL_CACHE.clear()

    middleware = AsyncModelOverrideMiddleware()
    request = _request(NovaAsyncContext(provider="opencode", model="deepseek-v4.1-flash"))

    seen: list[object] = []
    middleware.wrap_model_call(request, lambda req: seen.append(req.model) or AIMessage("ok"))

    assert seen == [fake_chat_model], "the handler did not receive the context's model"


def test_the_middleware_is_a_no_op_without_a_context():
    middleware = AsyncModelOverrideMiddleware()
    request = _request(None)

    seen: list[object] = []
    middleware.wrap_model_call(request, lambda req: seen.append(req.model) or AIMessage("ok"))

    assert seen == [request.model], "the model was replaced despite no context"


def test_a_per_agent_variable_still_wins(monkeypatch, fake_chat_model):
    """`PLAN_SCOUT_MODEL` was set for this one agent, so it outranks the shared role."""
    monkeypatch.setenv("PLAN_SCOUT_MODEL", "llama3:8b")
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, model: pytest.fail("the context must not be consulted"),
    )

    middleware = AsyncModelOverrideMiddleware(per_agent_model_var="PLAN_SCOUT_MODEL")
    request = _request(NovaAsyncContext(provider="opencode", model="deepseek-v4.1-flash"))

    seen: list[object] = []
    middleware.wrap_model_call(request, lambda req: seen.append(req.model) or AIMessage("ok"))

    assert seen == [request.model]


# ── the stored role ──────────────────────────────────────────────────────────


def test_an_unset_async_role_follows_the_main_model(monkeypatch):
    """Provider-agnostic by default: no async role means "whatever the session runs on"."""
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig.get_role_model",
        lambda self, role: None,
    )
    monkeypatch.delenv("ASYNC_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("ASYNC_AGENT_MODEL", raising=False)
    monkeypatch.setattr(
        "novacode_cli.utils.model_info.get_model_info",
        lambda: ("anthropic", "claude-sonnet-5-5", "Sonnet"),
    )
    context = current_async_context()
    assert (context.provider, context.model) == ("anthropic", "claude-sonnet-5-5")

    # An explicit environment setting is still the last word for the graphs.
    monkeypatch.setenv("ASYNC_AGENT_MODEL", "gpt-4o-mini")
    assert current_async_context() is None


def test_the_stored_async_role_becomes_the_run_context(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig.get_role_model",
        lambda self, role: {"provider": "opencode", "model": "deepseek-v4.1-flash"},
    )
    context = current_async_context()
    assert (context.provider, context.model) == ("opencode", "deepseek-v4.1-flash")


# ── the graph end of it ──────────────────────────────────────────────────────


def test_the_graph_actually_runs_on_the_context_model(monkeypatch):
    """The whole point: prove the run used the swapped model.

    Asserts on what executed (`ran-on:<id>`), not on the middleware's intent, so an
    override that is wired but ignored cannot pass. The builder is injected because a
    real one needs credentials; this test is about the graph honouring the context, not
    about provider plumbing, which `test_a_context_model_is_built_through_novas_own_builder`
    covers.
    """
    from deepagents import create_deep_agent

    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, model: _Named("spike-swapped"),
    )
    ac._MODEL_CACHE.clear()

    agent = create_deep_agent(
        name="spike",
        model=_Named("graph-default"),
        tools=[],
        middleware=[AsyncModelOverrideMiddleware()],
        context_schema=NovaAsyncContext,
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="hi")]},
        context=NovaAsyncContext(provider="opencode", model="spike-swapped"),
    )

    assert result["messages"][-1].content == "ran-on:spike-swapped"


def test_the_graph_keeps_its_own_model_when_no_context_arrives(monkeypatch):
    """Same graph, no context: the deployment's own model still runs."""
    from deepagents import create_deep_agent

    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_chat_model",
        lambda provider, model: pytest.fail("no context was sent, so nothing may be built"),
    )
    ac._MODEL_CACHE.clear()

    agent = create_deep_agent(
        name="spike",
        model=_Named("graph-default"),
        tools=[],
        middleware=[AsyncModelOverrideMiddleware()],
        context_schema=NovaAsyncContext,
    )

    result = agent.invoke({"messages": [HumanMessage(content="hi")]})

    assert result["messages"][-1].content == "ran-on:graph-default"


# ── the deepagents contract ──────────────────────────────────────────────────


def test_the_dispatch_contract_holds():
    """Canary for a deepagents/langgraph-sdk bump.

    The patch lives on private names, so if a bump moves the factories or drops
    `context` from `runs.create`, this fails here with a clear message instead of
    silently reverting every async agent to the container's boot-time model.
    """
    import inspect

    from deepagents.middleware import async_subagents
    from langgraph_sdk.client import RunsClient

    for name in ("get_client", "get_sync_client"):
        assert hasattr(async_subagents, name), (
            f"deepagents no longer exposes {name!r}; the Nova patch "
            "(novacode_cli.agents.async_context.install_async_model_context) needs updating"
        )

    assert "context" in inspect.signature(RunsClient.create).parameters, (
        "langgraph-sdk's runs.create no longer accepts `context`; async model "
        "override cannot be delivered"
    )


def test_the_run_context_is_actually_attached(monkeypatch):
    """The dispatch must carry the role.

    A patch that installs but never fires is worse than none, because it looks like it
    works.
    """
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig.get_role_model",
        lambda self, role: {"provider": "opencode", "model": "deepseek-v4.1-flash"},
    )

    sent: list[dict] = []

    class _Runs:
        def create(self, *args, **kwargs):  # noqa: ANN002, ANN003
            sent.append(kwargs)
            return {"run_id": "r"}

    client = ac._bind_context(type("_C", (), {})())
    client.runs = _Runs()
    client = ac._bind_context(client)
    client.runs.create(thread_id="t", assistant_id="a", input={})

    assert sent[0]["context"] == NovaAsyncContext(provider="opencode", model="deepseek-v4.1-flash")


def test_an_explicit_run_context_is_not_overwritten(monkeypatch):
    """A caller that named a context knows better than the stored role."""
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig.get_role_model",
        lambda self, role: {"provider": "opencode", "model": "deepseek-v4.1-flash"},
    )

    sent: list[dict] = []

    class _Runs:
        def create(self, *args: object, **kwargs: object) -> dict[str, str]:  # noqa: ARG002
            sent.append(dict(kwargs))
            return {"run_id": "r"}

    client = type("_C", (), {})()
    client.runs = _Runs()
    client = ac._bind_context(client)
    explicit = NovaAsyncContext(provider="ollama", model="stated")
    client.runs.create(thread_id="t", assistant_id="a", input={}, context=explicit)

    assert sent[0]["context"] is explicit


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_chat_model():
    return _Named("swapped")


def _request(context, model=None):
    """A ModelRequest of the shape the middleware sees, with *context* on its runtime.

    Every field is populated because ``request.override`` rebuilds the dataclass from
    all of them; a partial instance fails inside ``dataclasses.replace``.
    """
    from langchain.agents.middleware.types import ModelRequest

    return ModelRequest(
        model=model or _Named("request-default"),
        messages=[],
        system_message=None,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={},
        runtime=type("_R", (), {"context": context})(),
        model_settings={},
    )


def test_the_async_path_builds_the_model_off_the_event_loop(monkeypatch, fake_chat_model):
    """Building reads settings and key files: blocking calls `langgraph dev` rejects.

    On the loop the build raised BlockingError, was swallowed, and every async
    run fell back to the graph default. It must happen on a worker thread, and
    only once: later calls take the cached model without leaving the loop.
    """
    import asyncio
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(ac, "_MODEL_CACHE", {})
    built_on: list[str] = []

    def build(provider, model_name):  # noqa: ANN001, ANN202
        built_on.append(threading.current_thread().name)
        return fake_chat_model

    monkeypatch.setattr("novacode_cli.config.model_create.build_chat_model", build)

    class _Request:
        runtime = SimpleNamespace(context={"provider": "opencode", "model": "m"})
        model = None

        def override(self, *, model):  # noqa: ANN001, ANN202
            self.model = model
            return self

    async def run() -> tuple[str, list]:
        loop_thread = threading.current_thread().name
        seen = []

        async def handler(request):  # noqa: ANN001, ANN202
            seen.append(request.model)
            return "ok"

        mw = AsyncModelOverrideMiddleware()
        await mw.awrap_model_call(_Request(), handler)
        await mw.awrap_model_call(_Request(), handler)
        return loop_thread, seen

    loop_thread, seen = asyncio.run(run())
    assert seen == [fake_chat_model, fake_chat_model]
    assert len(built_on) == 1, "built once, then served from the cache"
    assert built_on[0] != loop_thread, "the build must not run on the event loop"

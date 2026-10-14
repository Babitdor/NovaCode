"""Review lifecycle tests use a deterministic provider with no network transport."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.store.memory import InMemoryStore

from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.review import ReviewRunner
from novacode_cli.hermes.tracker import ToolUsageTracker


class Request(SimpleNamespace):
    def override(self, **updates):
        return Request(**{**vars(self), **updates})


class Reviewer(FakeListChatModel):
    calls: list = []
    options: dict | None = None

    def __init__(self):
        super().__init__(responses=["unused"])

    def bind(self, **options):
        self.options = options
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        return AIMessage(
            '<lesson topic="testing">- run the local checks</lesson><habits>Always check tests</habits>'
        )


@pytest.mark.asyncio
async def test_reviews_prepare_candidates_without_global_changes(tmp_path):
    store = InMemoryStore()
    tracker = ToolUsageTracker(store)
    await tracker.record_tool_usage(
        "execute", True, outcome="unknown", evidence_source="structured"
    )
    model = Reviewer()
    runner = ReviewRunner(store, tracker, None, agent_dir=tmp_path, project="a")
    await runner.run_review(Request(model=model, messages=[HumanMessage("task")]))
    assert len(model.calls) == 1
    assert model.options == {"max_tokens": 4096}
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    assert ledger.list()
    assert all(r.status == "candidate" for r in ledger.list())
    assert ledger.list(project="a", active=True) == []
    assert not (tmp_path / "HABITS.md").exists()
    assert not (tmp_path / "skills").exists()
    assert not (tmp_path / "memories").exists()


@pytest.mark.asyncio
async def test_oversized_or_empty_evidence_skips_provider(tmp_path):
    store = InMemoryStore()
    tracker = ToolUsageTracker(store)
    model = Reviewer()
    runner = ReviewRunner(store, tracker, None, agent_dir=tmp_path, project="a")
    await runner.run_review(Request(model=model, messages=[HumanMessage("task")]))
    assert not model.calls
    await tracker.record_tool_usage("edit_file", True)
    await runner.run_review(Request(model=model, messages=[HumanMessage("large " * 100_000)]))
    assert not model.calls


@pytest.mark.asyncio
async def test_review_concurrency_is_two_process_wide_and_one_per_owner(tmp_path):
    entered = []
    release = asyncio.Event()
    ready = asyncio.Event()
    runners = []
    for i in range(3):
        store = InMemoryStore()
        runner = ReviewRunner(store, ToolUsageTracker(store), None, agent_dir=tmp_path / str(i))

        async def prepare(request, index=i):
            entered.append(index)
            if len(entered) == 2:
                ready.set()
            await release.wait()

        runner._prepare_review = prepare
        runners.append(runner)
    request = Request(model=Reviewer(), messages=[HumanMessage("task")])
    first = asyncio.create_task(runners[0].run_review(request))
    second = asyncio.create_task(runners[1].run_review(request))
    await asyncio.wait_for(ready.wait(), 5)
    third = asyncio.create_task(runners[2].run_review(request))
    await asyncio.sleep(0)
    await runners[0].run_review(request)
    assert sorted(entered) == [0, 1]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(second, third)
    assert sorted(entered) == [0, 1, 2]


@pytest.mark.asyncio
async def test_learning_registry_resolves_thread_owner_and_releases_queued_work(
    tmp_path, monkeypatch
):
    from weakref import WeakValueDictionary

    from novacode_cli.hermes import middleware
    from novacode_cli.session.runtime_identity import RuntimeIdentity

    monkeypatch.setattr(middleware, "_owners", WeakValueDictionary())
    store = InMemoryStore()
    first = middleware.NovaLearningMiddleware(
        store, agent_dir=tmp_path, project="project", owner="first"
    )
    second = middleware.NovaLearningMiddleware(
        store, agent_dir=tmp_path, project="project", owner="second"
    )
    assert middleware.get_active_learning_middleware() is None
    assert middleware.learning_middleware_for("project") is None
    assert middleware.learning_middleware_for("project", "first") is first
    assert middleware.learning_middleware_for("other", "first") is None
    assert first.for_session("first") is first

    agent = SimpleNamespace(config={"metadata": {"nova_learning_owner": "first"}})
    scoped = middleware.learning_middleware_for_agent(agent, "thread")
    session = RuntimeIdentity.from_config(
        "project", {"configurable": {"thread_id": "thread"}}, ""
    ).session
    assert scoped is first.for_session(session)
    pending = asyncio.create_task(asyncio.sleep(3600))
    scoped._refinement_tasks.add(pending)
    scoped._last_request = object()
    await first.release_session(session)
    assert pending.cancelled()
    assert scoped._last_request is None
    assert session not in first._sessions
    middleware.forget_learning_middleware("project", "first")
    assert middleware.get_active_learning_middleware() is second
    assert middleware.learning_middleware_for("project") is second
    middleware.forget_learning_middleware("project")
    assert middleware.get_active_learning_middleware() is None

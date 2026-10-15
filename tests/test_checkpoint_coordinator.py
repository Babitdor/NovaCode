"""Ownership, stale publication, and graph invocation coordination regressions."""

import asyncio

import aiosqlite
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

from novacode_cli.session.checkpoint_coordinator import (
    CheckpointCoordinator,
    CheckpointOwnershipError,
    CoordinatedGraph,
    CoordinatedSqliteSaver,
    current_lease,
    coordinate_saver,
)
from novacode_cli.session.runtime_identity import current_identity


@pytest.mark.asyncio
async def test_sqlite_fences_superseded_writer_and_pending_writes(tmp_path):
    path = str(tmp_path / "checkpoints.db")
    async with aiosqlite.connect(path) as first, aiosqlite.connect(path) as second:
        a, b = CoordinatedSqliteSaver(first), CoordinatedSqliteSaver(second)
        lease = await a.acquire("thread")
        with pytest.raises(CheckpointOwnershipError, match="active writer"):
            await b.acquire("thread")
        token = current_lease.set(lease)
        config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
        cp = empty_checkpoint()
        try:
            saved = await a.aput(config, cp, {}, {})
            await first.execute("UPDATE nova_leases SET expires=0")
            await first.commit()
            replacement = await b.acquire("thread")
            assert replacement.fence > lease.fence
            with pytest.raises(CheckpointOwnershipError, match="superseded"):
                await a.aput(config, empty_checkpoint(), {}, {})
            with pytest.raises(CheckpointOwnershipError, match="superseded"):
                await a.aput_writes(saved, [("messages", "lost")], "task")
            await a.release(lease)
            with pytest.raises(CheckpointOwnershipError, match="active writer"):
                await a.acquire("thread")
            state = await b.aget_tuple(saved)
            assert state.checkpoint["id"] == cp["id"]
            assert state.pending_writes == []
            await b.release(replacement)
        finally:
            current_lease.reset(token)


@pytest.mark.asyncio
async def test_shared_graph_identity_and_stale_update():
    class State(TypedDict):
        value: int

    seen = []

    async def node(state):
        seen.append(current_identity.get())
        await asyncio.sleep(0)
        return {"value": state["value"] + 1}

    saver = InMemorySaver()
    builder = StateGraph(State)
    builder.add_node("work", node)
    builder.set_entry_point("work")
    graph = CoordinatedGraph(
        builder.compile(checkpointer=saver), CheckpointCoordinator(saver), "project"
    )
    a, b = ({"configurable": {"thread_id": key}} for key in ("a", "b"))
    await asyncio.gather(graph.ainvoke({"value": 0}, a), graph.ainvoke({"value": 0}, b))
    assert {identity.thread for identity in seen} == {"a", "b"}
    assert len({identity.session for identity in seen}) == 2
    before = await graph.aget_state(a)
    revision = before.config["configurable"]["checkpoint_id"]
    await graph.ainvoke({"value": 2}, a)
    assert seen[0].session == seen[-1].session
    with pytest.raises(CheckpointOwnershipError, match="source checkpoint changed"):
        await graph.aupdate_state(a, {"value": 999}, expected_revision=revision)
    with pytest.raises(CheckpointOwnershipError, match="source checkpoint changed"):
        await graph.aupdate_state(before.config, {"value": 999}, expected_revision=revision)
    assert (await graph.aget_state(a)).values["value"] == 3
    assert graph.coordinator._locks == {}
    assert current_identity.get() is None


@pytest.mark.asyncio
async def test_sqlite_graph_saves_and_releases_on_interrupt(tmp_path):
    class State(TypedDict):
        value: int

    async with aiosqlite.connect(str(tmp_path / "graph.db")) as conn:
        saver = CoordinatedSqliteSaver(conn)
        builder = StateGraph(State)
        builder.add_node("work", lambda state: {"value": state["value"] + 1})
        builder.set_entry_point("work")
        graph = CoordinatedGraph(
            builder.compile(checkpointer=saver, interrupt_before=["work"]),
            CheckpointCoordinator(saver),
        )
        config = {"configurable": {"thread_id": "thread"}}
        await graph.ainvoke({"value": 0}, config)
        assert (await graph.aget_state(config)).next == ("work",)
        lease = await saver.acquire("thread")
        await saver.release(lease)
        await graph.ainvoke(None, config)
        assert (await graph.aget_state(config)).values["value"] == 1


@pytest.mark.asyncio
async def test_unknown_adapter_explicitly_restricted():
    coordinator = CheckpointCoordinator(object())
    with pytest.raises(CheckpointOwnershipError, match="atomic lease/fencing"):
        async with coordinator.mutation({"configurable": {"thread_id": "thread"}}, ""):
            pytest.fail("unsupported writer entered")


@pytest.mark.asyncio
async def test_mutation_requires_thread_and_serializes_waiting_writers():
    coordinator = CheckpointCoordinator(InMemorySaver())
    with pytest.raises(CheckpointOwnershipError, match="thread_id"):
        async with coordinator.mutation({"configurable": {}}, "project"):
            pytest.fail("unidentified writer entered")

    config = {"configurable": {"thread_id": "thread"}}
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def first():
        async with coordinator.mutation(config, "project"):
            order.append("first")
            entered.set()
            await release.wait()

    async def second():
        await entered.wait()
        async with coordinator.mutation(config, "project"):
            order.append("second")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    try:
        await entered.wait()
        await asyncio.sleep(0.03)
        assert order == ["first"]
        release.set()
        await asyncio.gather(*tasks)
        assert order == ["first", "second"]
        assert coordinator._locks == {}
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_activation_is_captured_for_one_mutation_and_released():
    from novacode_cli.hermes.activation import active_artifacts

    class Activation:
        def __init__(self):
            self.content = "first"

        def apply_pending(self):
            return (("prompt", "target", self.content),)

    coordinator = CheckpointCoordinator(InMemorySaver())
    activation = Activation()
    config = {"configurable": {"thread_id": "thread"}}
    async with coordinator.mutation(config, "project", activation):
        assert active_artifacts.get() == (("prompt", "target", "first"),)
        activation.content = "second"
        assert active_artifacts.get() == (("prompt", "target", "first"),)
    assert active_artifacts.get() == ()
    async with coordinator.mutation(config, "project", activation):
        assert active_artifacts.get() == (("prompt", "target", "second"),)
    assert active_artifacts.get() == ()


@pytest.mark.asyncio
async def test_event_stream_import_and_sync_mutations_keep_coordinator():
    class Graph:
        config = None

        def with_config(self, config):
            bound = Graph()
            bound.config = config
            return bound

        async def astream_events(self, _input, _config, **_kwargs):
            yield current_identity.get().thread

        async def abulk_update_state(self, _config, _supersteps):
            return current_identity.get().thread

    saver = InMemorySaver()
    graph = CoordinatedGraph(Graph(), CheckpointCoordinator(saver), "project")
    resources = [object()]
    graph.runtime_resources = resources
    bound = graph.with_config({"configurable": {"thread_id": "thread"}})
    assert bound.runtime_resources is resources
    assert [event async for event in bound.astream_events({"value": 1})] == ["thread"]
    assert await bound.abulk_update_state(None, []) == "thread"
    assert current_identity.get() is None
    assert graph.coordinator._locks == {}
    for method in (
        bound.invoke,
        bound.stream,
        bound.stream_events,
        bound.update_state,
        bound.bulk_update_state,
    ):
        with pytest.raises(CheckpointOwnershipError, match="coordinated async"):
            method(None)


@pytest.mark.asyncio
async def test_close_session_removes_resource_calibration_and_refusals():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from novacode_cli.session.runtime_identity import RuntimeIdentity

    session = RuntimeIdentity.from_config(
        "project", {"configurable": {"thread_id": "thread"}}, ""
    ).session
    resource = SimpleNamespace(
        release_session=AsyncMock(), _scales={session: 1.2}, _refusals={session: 3}
    )
    graph = CoordinatedGraph(object(), CheckpointCoordinator(InMemorySaver()), "project")
    graph.runtime_resources = [resource]
    await graph.close_session("thread")
    resource.release_session.assert_awaited_once_with(session)
    assert resource._scales == {}
    assert resource._refusals == {}


@pytest.mark.asyncio
async def test_existing_sqlite_adapter_is_wrapped_once_and_writes_under_lease(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "adapt.db")) as conn:
        saver = AsyncSqliteSaver(conn)
        coordinator = coordinate_saver(saver)
        assert isinstance(saver, CoordinatedSqliteSaver)
        assert coordinate_saver(saver) is coordinator
        config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
        async with coordinator.mutation(config, "project"):
            checkpoint = empty_checkpoint()
            await saver.aput(config, checkpoint, {}, {})
            assert (await saver.aget_tuple(config)).checkpoint["id"] == checkpoint["id"]
        assert current_lease.get() is None
        assert coordinator._locks == {}


@pytest.mark.asyncio
async def test_shared_graph_learning_sessions_and_resume_task(tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import ToolMessage
    from langgraph.store.memory import InMemoryStore
    from langgraph.types import Command, interrupt
    from novacode_cli.hermes.middleware import NovaLearningMiddleware
    from novacode_cli.session.runtime_identity import RuntimeIdentity

    class State(TypedDict):
        nova_task_identity: str
        value: int

    learning = NovaLearningMiddleware(InMemoryStore(), project="project", agent_dir=tmp_path)
    identities = []

    async def begin(state):
        return await learning.abefore_agent(state)

    async def work(state):
        identities.append(current_identity.get())

        async def result(request):
            return ToolMessage("verified", tool_call_id="call")

        await learning.awrap_tool_call(
            SimpleNamespace(tool_call={"id": "call", "name": "execute"}), result
        )
        return {"value": interrupt("review")}

    saver = InMemorySaver()
    builder = StateGraph(State)
    builder.add_node("begin", begin)
    builder.add_node("work", work)
    builder.set_entry_point("begin")
    builder.add_edge("begin", "work")
    graph = CoordinatedGraph(
        builder.compile(checkpointer=saver), CheckpointCoordinator(saver), "project"
    )
    graph.runtime_resources = [learning]
    a, b = ({"configurable": {"thread_id": key}} for key in ("a", "b"))
    await asyncio.gather(graph.ainvoke({"value": 0}, a), graph.ainvoke({"value": 0}, b))
    owners = [RuntimeIdentity.from_config("project", config, "").session for config in (a, b)]
    first, second = [learning.for_session(owner) for owner in owners]
    assert first is not second
    assert await first._tracker.get_call_count() == 1
    assert await second._tracker.get_call_count() == 1
    task = (await graph.aget_state(a)).values["nova_task_identity"]
    await graph.ainvoke(Command(resume=1), a)
    assert identities[-1].task == task
    assert await first._tracker.get_call_count() == 2
    assert await second._tracker.get_call_count() == 1
    await graph.close_session("a")
    assert owners[0] not in learning._sessions
    assert owners[1] in learning._sessions


@pytest.mark.asyncio
async def test_failed_sqlite_commit_preserves_checkpoint(tmp_path, monkeypatch):
    async with aiosqlite.connect(str(tmp_path / "fault.db")) as conn:
        saver = CoordinatedSqliteSaver(conn)
        coordinator = CheckpointCoordinator(saver)
        config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
        async with coordinator.mutation(config, ""):
            original = empty_checkpoint()
            await saver.aput(config, original, {}, {})
            commit = conn.commit

            async def fail_commit():
                if conn.in_transaction:
                    raise OSError("injected commit failure")
                await commit()

            monkeypatch.setattr(conn, "commit", fail_commit)
            with pytest.raises(OSError, match="commit failure"):
                await saver.aput(config, empty_checkpoint(), {}, {})
            monkeypatch.setattr(conn, "commit", commit)
            assert (await saver.aget_tuple(config)).checkpoint["id"] == original["id"]


@pytest.mark.asyncio
async def test_separate_process_cannot_claim_active_thread(tmp_path):
    import sys
    from pathlib import Path

    path = str(tmp_path / "process.db")
    async with aiosqlite.connect(path) as conn:
        saver = CoordinatedSqliteSaver(conn)
        lease = await saver.acquire("thread")
        script = Path(__file__).parent / "fixtures" / "checkpoint_writer.py"
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), path, stdout=asyncio.subprocess.PIPE
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 3
            assert b"active writer" in output
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await saver.release(lease)


@pytest.mark.asyncio
async def test_cancelled_stream_releases_ownership_and_context(tmp_path):
    class State(TypedDict):
        value: int

    entered = asyncio.Event()

    async def slow(state):
        entered.set()
        await asyncio.Event().wait()

    async with aiosqlite.connect(str(tmp_path / "cancel.db")) as conn:
        saver = CoordinatedSqliteSaver(conn)
        builder = StateGraph(State)
        builder.add_node("slow", slow)
        builder.set_entry_point("slow")
        graph = CoordinatedGraph(builder.compile(checkpointer=saver), CheckpointCoordinator(saver))
        config = {"configurable": {"thread_id": "thread"}}

        async def consume():
            async for _ in graph.astream({"value": 0}, config):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert graph.coordinator._locks == {}
        assert current_lease.get() is None
        assert current_identity.get() is None
        lease = await saver.acquire("thread")
        await saver.release(lease)


@pytest.mark.asyncio
async def test_renewal_and_delete_require_live_matching_ownership(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "renew.db")) as conn:
        saver = CoordinatedSqliteSaver(conn)
        config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
        lease = await saver.acquire("thread")
        with pytest.raises(CheckpointOwnershipError, match="coordinated ownership"):
            await saver.aput(config, empty_checkpoint(), {}, {})
        token = current_lease.set(lease)
        try:
            await saver.aput(config, empty_checkpoint(), {}, {})
            await saver.renew(lease)
            await conn.execute("UPDATE nova_leases SET expires=0")
            await conn.commit()
            # Lapsed but unclaimed (a stalled heartbeat): still the newest fence,
            # so the write goes through and the lease is extended.
            await saver.aput(config, empty_checkpoint(), {}, {})
            await saver.renew(lease)
            await conn.execute("UPDATE nova_leases SET expires=0")
            await conn.commit()
            taken = await saver.acquire("thread")  # now someone takes it over
            with pytest.raises(CheckpointOwnershipError, match="superseded"):
                await saver.renew(lease)
            with pytest.raises(CheckpointOwnershipError, match="superseded"):
                await saver.adelete_thread("thread")
            assert await saver.aget_tuple(config) is not None
            await saver.release(taken)
        finally:
            current_lease.reset(token)
        async with CheckpointCoordinator(saver).mutation(config, ""):
            await saver.adelete_thread("thread")
        assert await saver.aget_tuple(config) is None

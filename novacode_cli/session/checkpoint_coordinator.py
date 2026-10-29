"""Coordinate complete graph mutations and fence SQLite checkpoint publication."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

# JSON and LangGraph interfaces carry heterogeneous values.
# ruff: noqa: ANN401
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from novacode_cli.session.runtime_identity import RuntimeIdentity, current_identity


class CheckpointOwnershipError(RuntimeError):
    """A writer no longer owns its thread; history must remain unchanged."""


@dataclass(frozen=True)
class Lease:
    """One renewable ownership claim with a monotonically advancing fence."""

    thread: str
    owner: str
    fence: int


current_lease: ContextVar[Lease | None] = ContextVar("nova_checkpoint_lease", default=None)


class CoordinatedSqliteSaver(AsyncSqliteSaver):
    """SQLite ownership validation and checkpoint writes share one transaction."""

    lease_seconds = 30.0

    async def setup(self) -> None:
        """Initialize the ownership table alongside the checkpoint schema."""
        await super().setup()
        async with self.lock:
            await self.conn.execute(
                "CREATE TABLE IF NOT EXISTS nova_leases "
                "(thread TEXT PRIMARY KEY, owner TEXT, fence INTEGER NOT NULL, expires REAL)"
            )
            await self.conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Hold the saver connection lock through validation and commit."""
        await self.setup()
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def acquire(self, thread: str) -> Lease:
        """Claim a free or expired thread and advance its fencing token."""
        owner = uuid.uuid4().hex
        async with self.transaction():
            async with self.conn.execute(
                "SELECT owner,fence,expires FROM nova_leases WHERE thread=?", (thread,)
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0] and row[2] > time.time():
                message = "Thread has an active writer; retry after it pauses"
                raise CheckpointOwnershipError(message)
            fence = row[1] + 1 if row else 1
            await self.conn.execute(
                "INSERT OR REPLACE INTO nova_leases VALUES (?,?,?,?)",
                (thread, owner, fence, time.time() + self.lease_seconds),
            )
        return Lease(thread, owner, fence)

    async def validate(self, thread: str) -> None:
        """Reject an absent or superseded lease; extend one that merely lapsed.

        Expiry exists so another writer may take over from a stuck one. Until
        one does, this lease's fence is still the newest, and validation and
        ``acquire`` both run inside ``BEGIN IMMEDIATE``, so they cannot
        interleave: either the takeover came first (the fence changed, and this
        writer is refused) or this write comes first and extends the lease (and
        the would-be taker sees an active writer). Refusing a lapsed but
        unclaimed lease added no safety; it turned a stalled heartbeat -- the
        UI loop frozen for ~23 s on a long transcript -- into a lost save.
        Callers hold the transaction (see :meth:`transaction`).
        """
        lease = current_lease.get()
        if lease is None or lease.thread != str(thread):
            message = "Checkpoint mutation requires coordinated ownership"
            raise CheckpointOwnershipError(message)
        async with self.conn.execute(
            "SELECT owner,fence,expires FROM nova_leases WHERE thread=?", (lease.thread,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] != lease.owner or row[1] != lease.fence:
            message = "Checkpoint lease was superseded by another writer"
            raise CheckpointOwnershipError(message)
        if row[2] <= time.time():
            await self.conn.execute(
                "UPDATE nova_leases SET expires=? WHERE thread=? AND owner=? AND fence=?",
                (time.time() + self.lease_seconds, lease.thread, lease.owner, lease.fence),
            )

    async def renew(self, lease: Any) -> None:
        """Extend only the current live lease."""
        async with self.transaction():
            await self.validate(lease.thread)
            await self.conn.execute(
                "UPDATE nova_leases SET expires=? WHERE thread=? AND owner=? AND fence=?",
                (time.time() + self.lease_seconds, lease.thread, lease.owner, lease.fence),
            )

    async def release(self, lease: Any) -> None:
        """Release only this token, preserving any replacement writer."""
        async with self.transaction():
            await self.conn.execute(
                "UPDATE nova_leases SET owner=NULL,expires=0 "
                "WHERE thread=? AND owner=? AND fence=?",
                (lease.thread, lease.owner, lease.fence),
            )

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        """Publish a checkpoint under transactionally validated ownership."""
        del new_versions  # SQLite stores full checkpoints, unlike memory's version blobs.
        import json

        from langgraph.checkpoint.base import get_checkpoint_metadata

        values = config["configurable"]
        kind, payload = self.serde.dumps_typed(checkpoint)
        async with self.transaction():
            await self.validate(values["thread_id"])
            await self.conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,"
                "checkpoint,metadata) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(values["thread_id"]),
                    values.get("checkpoint_ns", ""),
                    checkpoint["id"],
                    values.get("checkpoint_id"),
                    kind,
                    payload,
                    json.dumps(get_checkpoint_metadata(config, metadata), ensure_ascii=False),
                ),
            )
        return {
            "configurable": {
                "thread_id": values["thread_id"],
                "checkpoint_ns": values.get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self, config: Any, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        """Fence intermediate writes, including persisted approvals."""
        del task_path  # The upstream SQLite schema does not persist task paths.
        from langgraph.checkpoint.base import WRITES_IDX_MAP

        values = config["configurable"]
        query = (
            "INSERT OR REPLACE INTO writes "
            "(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) "
            "VALUES (?,?,?,?,?,?,?,?)"
            if all(w[0] in WRITES_IDX_MAP for w in writes)
            else "INSERT OR IGNORE INTO writes "
            "(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) "
            "VALUES (?,?,?,?,?,?,?,?)"
        )
        async with self.transaction():
            await self.validate(values["thread_id"])
            await self.conn.executemany(
                query,
                [
                    (
                        str(values["thread_id"]),
                        values.get("checkpoint_ns", ""),
                        values["checkpoint_id"],
                        task_id,
                        WRITES_IDX_MAP.get(channel, index),
                        channel,
                        *self.serde.dumps_typed(value),
                    )
                    for index, (channel, value) in enumerate(writes)
                ],
            )

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete a thread only while owning its lease."""
        async with self.transaction():
            await self.validate(thread_id)
            await self.conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            await self.conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))


class CheckpointCoordinator:
    """One coordinator per saver; graph bindings share the same thread locks."""

    def __init__(self, saver: Any) -> None:
        """Initialize local state without acquiring invocation ownership."""
        self.saver = saver
        self._locks: dict[str, tuple[Any, int]] = {}
        self._locks_guard = threading.Lock()

    @asynccontextmanager
    async def mutation(
        self, config: Any, project: str, activation: Any = None
    ) -> AsyncIterator[None]:
        """Serialize writers and bind immutable invocation configuration."""
        thread = str(config.get("configurable", {}).get("thread_id", ""))
        if not thread:
            message = "Coordinated mutations require thread_id"
            raise CheckpointOwnershipError(message)
        if not isinstance(self.saver, (CoordinatedSqliteSaver, InMemorySaver)):
            message = (
                "Persistence adapter lacks atomic lease/fencing support; "
                "concurrent writers disabled"
            )
            raise CheckpointOwnershipError(message)
        async with self._locked(thread), self._invocation(config, project, activation):
            yield

    @asynccontextmanager
    async def _locked(self, thread: str) -> AsyncIterator[None]:
        with self._locks_guard:
            lock, users = self._locks.setdefault(thread, (threading.Lock(), 0))
            self._locks[thread] = (lock, users + 1)
        try:
            acquired = False
            try:
                # threading locks also protect separate event loops in this
                # process; an asyncio.Event cannot cross those loop boundaries.
                while not lock.acquire(blocking=False):  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
                acquired = True
                yield
            finally:
                if acquired:
                    lock.release()
        finally:
            with self._locks_guard:
                lock, users = self._locks[thread]
                if users == 1:
                    del self._locks[thread]
                else:
                    self._locks[thread] = (lock, users - 1)

    @asynccontextmanager
    async def _invocation(self, config: Any, project: str, activation: Any) -> AsyncIterator[None]:
        from novacode_cli.hermes.activation import active_artifacts

        thread = str(config["configurable"]["thread_id"])
        lease = (
            await self.saver.acquire(thread)
            if isinstance(self.saver, CoordinatedSqliteSaver)
            else None
        )
        token = current_lease.set(lease)
        identity_token = current_identity.set(
            RuntimeIdentity.from_config(project, config, uuid.uuid4().hex)
        )
        heartbeat = asyncio.create_task(self._heartbeat(lease)) if lease else None
        activation_token = None
        try:
            if activation:
                artifacts = await asyncio.to_thread(activation.apply_pending)
                activation_token = active_artifacts.set(artifacts)
            yield
            if heartbeat and heartbeat.done():
                heartbeat.result()
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                if lease:
                    await self.saver.release(lease)
            finally:
                if activation_token is not None:
                    active_artifacts.reset(activation_token)
                current_identity.reset(identity_token)
                current_lease.reset(token)

    async def _heartbeat(self, lease: Any) -> None:
        while True:
            await asyncio.sleep(self.saver.lease_seconds / 3)
            await self.saver.renew(lease)


def coordinate_saver(saver: Any) -> CheckpointCoordinator:
    """Adapt only known local savers; remote savers remain explicitly restricted."""
    if type(saver) is AsyncSqliteSaver:
        # Both adapters use the same connection lock; no competing transaction
        # may run between ownership validation and publication.
        saver.__class__ = CoordinatedSqliteSaver
    coordinator = getattr(saver, "nova_coordinator", None)
    if coordinator is None:
        coordinator = saver.nova_coordinator = CheckpointCoordinator(saver)
    return coordinator


class CoordinatedGraph:
    """Route public async mutations through the saver coordinator."""

    def __init__(
        self, graph: Any, coordinator: Any, project: str = "", activation: Any = None
    ) -> None:
        """Initialize local state without acquiring invocation ownership."""
        self.graph, self.coordinator, self.project = graph, coordinator, project
        self.activation = activation
        self.runtime_resources: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        """Delegate read-only graph operations and inspection."""
        return getattr(self.graph, name)

    def with_config(self, *args: Any, **kwargs: Any) -> CoordinatedGraph:
        """Preserve coordination when binding graph configuration."""
        bound = type(self)(
            self.graph.with_config(*args, **kwargs), self.coordinator, self.project, self.activation
        )
        bound.runtime_resources = self.runtime_resources
        bound.agent_definitions_signature = getattr(self, "agent_definitions_signature", None)
        bound.agent_definitions_workspace = getattr(self, "agent_definitions_workspace", None)
        bound.agent_definition_options = getattr(self, "agent_definition_options", {})
        return bound

    async def close_session(self, thread: str) -> None:
        """Release session-owned auxiliary tasks and model calibration."""
        identity = RuntimeIdentity.from_config(
            self.project, {"configurable": {"thread_id": thread}}, ""
        )
        from novacode_cli.events import nova_event_log
        from novacode_cli.security.session_allow import release_session_allow
        from novacode_cli.tools.python_kernel_tool import release_session_kernel

        await release_session_kernel(identity.session)
        release_session_allow(identity.session)
        nova_event_log.release_session(identity.session)
        for resource in self.runtime_resources:
            release = getattr(resource, "release_session", None)
            if release:
                await release(identity.session)
            scales = getattr(resource, "_scales", None)
            if scales is not None:
                scales.pop(identity.session, None)
            refusals = getattr(resource, "_refusals", None)
            if refusals is not None:
                refusals.pop(identity.session, None)
            accepted = getattr(resource, "_accepted", None)
            if accepted is not None:
                accepted.pop(identity.session, None)

    def _config(self, config: Any) -> Any:
        from langchain_core.runnables.config import merge_configs

        return merge_configs(getattr(self.graph, "config", None), config)

    async def _bind_resume_task(self, value: Any, config: Any) -> None:
        from langgraph.types import Command
        if value is None or isinstance(value, Command):
            state = await self.graph.aget_state(config)
            task = state.values.get("nova_task_identity")
            identity = current_identity.get()
            if task and identity:
                current_identity.set(replace(identity, task=task))

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:  # noqa: A002 - LangGraph public argument
        """Hold ownership until streaming ends or pauses."""
        config = self._config(config)
        async with self.coordinator.mutation(config, self.project, self.activation):
            await self._bind_resume_task(input, config)
            async for item in self.graph.astream(input, config, **kwargs):
                yield item

    async def astream_events(
        self,
        input: Any,  # noqa: A002 - LangGraph public argument
        config: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Hold ownership across event streaming."""
        config = self._config(config)
        async with self.coordinator.mutation(config, self.project, self.activation):
            await self._bind_resume_task(input, config)
            async for item in self.graph.astream_events(input, config, **kwargs):
                yield item

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:  # noqa: A002 - LangGraph public argument
        """Coordinate a complete graph invocation."""
        config = self._config(config)
        async with self.coordinator.mutation(config, self.project, self.activation):
            await self._bind_resume_task(input, config)
            return await self.graph.ainvoke(input, config, **kwargs)

    async def aupdate_state(
        self,
        config: Any,
        values: Any,
        as_node: Any = None,
        *,
        expected_revision: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Commit a state update only against a current source revision."""
        config = self._config(config)
        async with self.coordinator.mutation(config, self.project):
            if expected_revision is not None:
                # A captured config may itself pin the old checkpoint. Compare
                # with the latest thread revision, not that historical view.
                latest_config = {**config, "configurable": dict(config.get("configurable", {}))}
                latest_config["configurable"].pop("checkpoint_id", None)
                state = await self.graph.aget_state(latest_config)
                revision = (state.config or {}).get("configurable", {}).get("checkpoint_id")
                if revision != expected_revision:
                    message = "Compaction source checkpoint changed; preparation discarded"
                    raise CheckpointOwnershipError(message)
            return await self.graph.aupdate_state(config, values, as_node=as_node, **kwargs)

    async def abulk_update_state(self, config: Any, supersteps: Any) -> Any:
        """Coordinate imported batches of checkpoint mutations."""
        config = self._config(config)
        async with self.coordinator.mutation(config, self.project):
            return await self.graph.abulk_update_state(config, supersteps)

    def _sync_restricted(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        message = "Use the coordinated async graph interface for mutations"
        raise CheckpointOwnershipError(message)

    invoke = stream = stream_events = update_state = bulk_update_state = _sync_restricted

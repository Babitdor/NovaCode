"""Persistent in-process teammates with a private graph and serialized inbox."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from novacode_cli.teams.gateway import Binding

if TYPE_CHECKING:
    from collections.abc import Callable

    from novacode_cli.states.Session import SessionState
    from novacode_cli.teams.models import SpawnRequest


class InProcessAdapter:
    """Build a fresh configured graph; share only explicit runtime services."""

    def __init__(self, builder: Callable, *, skip_file_mentions: bool = False) -> None:
        """Inject the existing Nova graph builder, not another agent loop."""
        self.builder = builder
        self.skip_file_mentions = skip_file_mentions

    async def start(
        self, request: SpawnRequest, event: Callable, gateway: Callable
    ) -> InProcessHandle:
        """Construct the child without starting its first model call."""
        binding = Binding(request.identity, gateway)
        preparation = asyncio.create_task(asyncio.to_thread(self.builder, request, binding))
        try:
            agent, backend, state = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            agent, backend, state = await preparation
            if hasattr(agent, "close_session"):
                await agent.close_session(state.thread_id)
            raise
        try:
            if request.messages:
                await agent.aupdate_state(
                    {"configurable": {"thread_id": state.thread_id}},
                    {"messages": list(request.messages)},
                )
        except BaseException:
            if hasattr(agent, "close_session"):
                await agent.close_session(state.thread_id)
            raise
        return InProcessHandle(
            request, agent, backend, state, event, skip_file_mentions=self.skip_file_mentions
        )


class InProcessHandle:
    """One private invocation queue and cancellation scope for one teammate."""

    def __init__(
        self,
        request: SpawnRequest,
        agent: Any,  # noqa: ANN401 - Graph wrapper.
        backend: Any,  # noqa: ANN401 - Backend implementations.
        state: SessionState,
        event: Callable,
        *,
        skip_file_mentions: bool = False,
    ) -> None:
        """Start only the idle inbox driver, with bounded pending work."""
        self.request, self.agent, self.backend, self.state, self.event = (
            request,
            agent,
            backend,
            state,
            event,
        )
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.driver = asyncio.create_task(self._drive(), name=f"teammate-{request.identity.agent}")
        self.stopped = False
        self.turns = 0
        self.skip_file_mentions = skip_file_mentions
        import os

        import psutil

        self.recovery = {
            "kind": "inprocess",
            "pid": os.getpid(),
            "born": psutil.Process().create_time(),
        }

    async def send(self, text: str) -> None:
        """Queue one wake-up; the driver never invokes the graph concurrently."""
        if self.stopped:
            msg = "Teammate has stopped"
            raise RuntimeError(msg)
        from novacode_cli.teams.models import Limits, TeamError

        if self.turns >= Limits().turns:
            message = "Teammate turn limit reached; review and start a fresh team"
            raise TeamError(message)
        self.queue.put_nowait(text)
        self.turns += 1

    async def _drive(self) -> None:  # noqa: PLR0912, PLR0915 - Explicit lifecycle transitions.
        import uuid

        from novacode_cli import ui_events as ev
        from novacode_cli.core.agent_loop import iterate_agent_events
        from novacode_cli.tracking.usage_tree import (
            UsageTree,
            get_current_tree,
            set_current_tree,
            usage_scope,
        )

        while True:
            text = await self.queue.get()
            summary, error = [], ""
            previous_tree = get_current_tree()
            tree = UsageTree(self.state.thread_id)
            set_current_tree(tree)
            turn_id = uuid.uuid4().hex
            try:
                await self.event("running", {})
                config = {"configurable": {"thread_id": self.state.thread_id}}
                before = await self.agent.aget_state(config)
                old_ids = {message.id for message in before.values.get("messages", [])}
                cancelled = False
                with usage_scope(f"team:{self.request.identity.agent}"):
                    async for event in iterate_agent_events(
                        text,
                        self.agent,
                        self.request.agent_type,
                        self.state,
                        backend=self.backend,
                        skip_file_mentions=self.skip_file_mentions,
                    ):
                        if isinstance(event, ev.InterruptRequest):
                            # Tool approvals use the broker; child plan/question
                            # interrupts are never silently approved.
                            if not event.future.done():
                                from novacode_cli.core.agent_loop import default_interrupt_response

                                event.future.set_result(default_interrupt_response(event.kind))
                        elif isinstance(event, ev.AssistantMessage):
                            summary.append(event.text)
                        elif isinstance(event, ev.Error):
                            error = event.message
                        elif isinstance(event, ev.Cancelled):
                            # The shared loop reports cancellation as an event.
                            # Preserve adapter cancellation instead of recording
                            # a false success and waiting on the inbox forever.
                            cancelled = True
                if cancelled:
                    raise asyncio.CancelledError  # noqa: TRY301
                if not summary and not error:
                    # Non-streaming model implementations can commit an answer
                    # without token events. Recover only this turn's final AI.
                    from langchain_core.messages import AIMessage

                    after = await self.agent.aget_state(config)
                    for message in reversed(after.values.get("messages", [])):
                        if (
                            isinstance(message, AIMessage)
                            and message.id not in old_ids
                            and not message.tool_calls
                        ):
                            if message.text:
                                summary.append(message.text)
                            break
            except asyncio.CancelledError:
                await self.event("stopping", {"usage": tree.to_dict(), "usage_turn": turn_id})
                set_current_tree(previous_tree)
                self.queue.task_done()
                raise
            except Exception as exc:  # noqa: BLE001 — report failure without killing the lead
                error = str(exc)
            try:
                await self.event(
                    "failed" if error else "idle",
                    {
                        "full_output": "\n".join(summary),
                        "error": error,
                        "model": (
                            getattr(self.state, "team_model_name", None)
                            or getattr(self.state, "session_model_provider", None)
                        ),
                        "usage": tree.to_dict(),
                        "usage_turn": turn_id,
                    },
                )
            finally:
                set_current_tree(previous_tree)
                self.queue.task_done()
            if error:
                return

    async def stop(self) -> bool:
        """Cancel and join this driver's graph; never another teammate's task."""
        self.stopped = True
        self.driver.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(self.driver, return_exceptions=True), timeout=10)
        except TimeoutError:
            return False
        if hasattr(self.agent, "close_session"):
            await self.agent.close_session(self.state.thread_id)
        return True

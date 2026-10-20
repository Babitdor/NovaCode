"""Keep graph lifetime separate from conversation ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.teams.gateway import Binding
from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from collections.abc import Callable

    from novacode_cli.states.Session import SessionState
    from novacode_cli.teams.coordinator import TeamCoordinator


class TeamHost:
    """Resolve immutable per-thread bindings when a graph survives /clear."""

    def __init__(self, initial: TeamCoordinator, factory: Callable, register: Callable) -> None:
        """Keep roots alive until their own conversation closes."""
        self.initial = initial
        self.factory = factory
        self.register = register
        self.roots = {initial.lead.root: initial}
        self.threads: dict[str, str] = {}

    def bind_session(self, state: SessionState) -> TeamCoordinator:
        """Bind a trusted session before its graph begins executing."""
        root = state.session_id
        coordinator = self.roots.get(root)
        if coordinator is None or coordinator._closed:
            coordinator = self.factory(root)
            self.roots[root] = coordinator
            self.register(root, self)
        coordinator.session_state = state
        self.threads[state.thread_id] = root
        return coordinator

    def binding(self, thread: str | None) -> Binding:
        """Each async invocation resolves its own coordinator; no active pointer."""
        if thread is not None and thread not in self.threads:
            message = "Conversation is not bound to this team host"
            raise TeamError(message)
        root = self.threads.get(thread or "", self.initial.lead.root)
        coordinator = self.roots.get(root, self.initial)

        async def endpoint(operation: str, payload: dict) -> dict:
            return await coordinator.request(coordinator.lead, operation, payload)

        return Binding(coordinator.lead, endpoint, coordinator)

    async def release_session(self, thread: str) -> None:
        """Closing one conversation cannot close another conversation's workers."""
        root = self.threads.get(thread)
        if root is not None:
            await self.roots[root].close()
            self.threads.pop(thread, None)
            if root != self.initial.lead.root:
                self.roots.pop(root, None)
            self.register(root, None)
            from novacode_cli.agents.agent_mailbox import clear_scoped_mailbox

            clear_scoped_mailbox(self.initial.lead.project, root)

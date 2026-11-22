"""Which sessions one server holds, and how a client moves between them.

A connection has always owned exactly one session (ADR-005). This module is the
bookkeeping that lifts that limit in stages: every host registers here, the
`sessions` frame is answered from here, and `attach` switches a connection's
active host by id instead of answering "not yet".

What is deliberately *not* here: spawning. Creating a session still goes through
`novacode_cli.sessions.supervisor` in the wave that owns tabs, so this registry
only ever reports sessions that already exist and can be reached.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from novacode_cli.sessions.supervisor import MAX_SESSIONS

if TYPE_CHECKING:
    from novacode_cli.server.host import SessionHost

logger = logging.getLogger(__name__)

# Sessions one server may hold. Reused from the supervisor rather than restated,
# so a client sees one capacity number however it asks.
CAPACITY = MAX_SESSIONS


class SessionCapacityError(RuntimeError):
    """Raised when a server already holds :data:`CAPACITY` sessions."""


class SessionRegistry:
    """The session hosts this server holds, keyed by session id.

    Args:
        capacity: Sessions to allow before :meth:`add` refuses. Defaults to the
            supervisor's limit.
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        """Create an empty registry with the given capacity."""
        self.capacity = capacity
        self._hosts: dict[str, SessionHost] = {}

    def __len__(self) -> int:
        """Number of sessions currently held."""
        return len(self._hosts)

    def __contains__(self, session_id: object) -> bool:
        """Whether a session id is held."""
        return isinstance(session_id, str) and session_id in self._hosts

    def has_room(self) -> bool:
        """Whether another session may register.

        Returns:
            ``True`` while the registry holds fewer than ``capacity`` sessions.
        """
        return len(self._hosts) < self.capacity

    def add(self, host: SessionHost) -> None:
        """Register one host under its session id.

        Args:
            host: A host that has a spec, so its id is known.

        Raises:
            SessionCapacityError: When the registry is already full.
        """
        session_id = host.session_id
        if session_id is None:
            logger.debug("host registered without a session id; skipping")
            return
        if not self.has_room() and session_id not in self._hosts:
            msg = f"server holds {len(self._hosts)} sessions, the limit is {self.capacity}"
            raise SessionCapacityError(msg)
        self._hosts[session_id] = host

    def remove(self, host: SessionHost) -> None:
        """Forget a host, keeping a replacement registered under the same id.

        Args:
            host: The host being closed. A newer host that took over the id is
                left in place.
        """
        session_id = host.session_id
        if session_id is not None and self._hosts.get(session_id) is host:
            del self._hosts[session_id]

    def get(self, session_id: str) -> SessionHost | None:
        """Return the host for *session_id*, or ``None``.

        Args:
            session_id: Session id to look up.
        """
        return self._hosts.get(session_id)

    def summary(self) -> list[dict[str, Any]]:
        """Build the rows a ``sessions`` frame carries.

        Returns:
            One row per held session, in registration order.
        """
        return [host.row() for host in self._hosts.values()]

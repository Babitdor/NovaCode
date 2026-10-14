"""Immutable invocation identity, inherited by graph tasks through contextvars."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass

# JSON and LangGraph interfaces carry heterogeneous values.


@dataclass(frozen=True)
class RuntimeIdentity:
    """Identity of a project session and one checkpoint invocation."""

    project: str
    session: str
    checkpoint_namespace: str
    thread: str
    task: str

    @classmethod
    def from_config(cls, project: str, config: dict, task: str) -> RuntimeIdentity:
        """Resolve the local from config operation."""
        values = config.get("configurable", {})
        thread = str(values["thread_id"])
        namespace = str(values.get("checkpoint_ns", ""))
        session = hashlib.sha256(f"{project}\0{thread}".encode()).hexdigest()
        return cls(project, session, namespace, thread, task)


current_identity: ContextVar[RuntimeIdentity | None] = ContextVar("nova_runtime", default=None)

"""Live interpreter approvals owned by one shared-loop invocation."""
# ruff: noqa: ANN401, EM101, TRY003

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable


class CodeApprovals:
    """Keep futures in the host; custom stream events carry only opaque IDs."""

    def __init__(self, valid: Callable[[], bool] = lambda: True) -> None:
        """Start a broker scoped to one live invocation."""
        self.pending: dict[str, tuple[dict, asyncio.Future]] = {}
        self.active = True
        self.valid = valid

    def ensure_active(self) -> None:
        """Reject a stale conversation even if an approval just completed."""
        if not self.active or not self.valid():
            self.close()
            raise asyncio.CancelledError

    async def request(self, writer: Any, payload: dict) -> dict:
        """Suspend this action until its owning consumer answers."""
        if not self.active or writer is None:
            raise ValueError("Code Mode requires an active approval handler.")
        self.ensure_active()
        action_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[action_id] = (payload, future)
        try:
            writer({"type": "nova_code_approval", "id": action_id})
            response = await future
            self.ensure_active()
            return response
        finally:
            self.pending.pop(action_id, None)

    def close(self) -> None:
        """Invalidate pending decisions when the invocation ends or is cancelled."""
        self.active = False
        for _, future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()


current_code_approvals: ContextVar[CodeApprovals | None] = ContextVar(
    "current_code_approvals", default=None
)

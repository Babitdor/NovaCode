"""Cleanup-only reconciliation: uncertain work is never automatically replayed."""

from __future__ import annotations

import asyncio
from typing import Any

from novacode_cli.teams.models import TeamError


def stop_process(record: dict[str, Any]) -> bool:
    """Verify PID birth and attempt identity before touching an orphan process."""
    import psutil

    try:
        process = psutil.Process(record["pid"])
        if process.create_time() != record["born"]:
            return True  # The original worker is gone; never signal a reused PID.
        if record["kind"] == "inprocess":
            return False  # A surviving host may still own its async task.
        if process.environ().get("NOVA_TEAM_ATTEMPT") != record["attempt"]:
            return False
        children = process.children(recursive=True)
        for child in reversed(children):
            child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs([*children, process], timeout=5)
        for child in alive:
            child.kill()
        _, alive = psutil.wait_procs(alive, timeout=5)
        return not alive  # noqa: TRY300 - Keep launch and cleanup in one guard.
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, KeyError, TypeError):
        return False


class RecoveryHandle:
    """Retain capacity until the owned worker is proved stopped."""

    def __init__(self, record: dict[str, Any]) -> None:
        """Retain the original ownership proof and a cleanup-only reservation."""
        self.recovery = record
        from novacode_cli.teams.scheduler import PROCESS_BUDGET

        self._budget_key = f"recovery:{id(self)}"
        if record.get("kind") != "inprocess":
            PROCESS_BUDGET.adopt(self._budget_key)
        self.cleanup_note = (
            "Preserved workspace after restart; inspect partial work before reassignment."
        )

    async def send(self, _text: str) -> None:
        """Never replay model work on a disconnected transport."""
        msg = "Disconnected workers require reconciliation; no task was replayed"
        raise TeamError(msg)

    async def stop(self) -> bool:
        """Release the reservation only on positive evidence of worker termination."""
        from novacode_cli.teams.scheduler import PROCESS_BUDGET

        certain = await self._stop()
        if certain:
            PROCESS_BUDGET.release(self._budget_key)
        return certain

    async def _stop(self) -> bool:
        """Only recognized, verifiable ownership records can prove termination."""
        if self.recovery.get("kind") == "absent":
            return True
        if self.recovery.get("kind") == "container":
            from novacode_cli.teams.adapters.container import stop_owned_container

            return await stop_owned_container(self.recovery["name"], self.recovery["labels"])
        if self.recovery.get("kind") in {"process", "inprocess"}:
            return await asyncio.to_thread(stop_process, self.recovery)
        return False

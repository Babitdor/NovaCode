"""Reconcile the crash window between durable launch intent and a recorded PID."""

from __future__ import annotations

import asyncio
from typing import Any

from novacode_cli.teams.recovery import stop_process


def _find_and_stop(record: dict[str, Any]) -> bool:
    """Use the persisted owner, birth bound and nonce; never signal a reused identity."""
    import psutil

    candidates = []
    uncertain = False
    for process in psutil.process_iter(["pid", "username", "create_time"], ad_value=None):
        if process.info["username"] != record["owner"]:
            continue
        born = process.info["create_time"]
        if born is None:
            uncertain = True
            continue
        if born < record["since"] - 1:
            continue
        try:
            if process.environ().get("NOVA_TEAM_ATTEMPT") == record["attempt"]:
                candidates.append(
                    {
                        "kind": "process",
                        "pid": process.pid,
                        "born": born,
                        "attempt": record["attempt"],
                    }
                )
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            uncertain = True
    results = [stop_process(candidate) for candidate in candidates]
    return not uncertain and all(results)


async def reconcile_start(record: dict[str, Any]) -> bool:
    """Prove startup ownership ended before allowing another attempt to mutate its files."""
    if record.get("container"):
        from novacode_cli.teams.adapters.container import stop_owned_container

        return await stop_owned_container(record["name"], record["labels"])
    return await asyncio.to_thread(_find_and_stop, record)

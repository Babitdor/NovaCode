"""Execute durable proactive agents outside any chat transcript.

The store owns scheduling; this module owns the two loops that make a saved
agent actually run:

* ``_runs`` claims one queued job at a time and runs it with a fresh, read-only
  graph;
* ``_tags`` polls GitHub for a watched repository and queues a run when a new tag
  appears.

Nothing here imports Textual, so the socket server can host the same runtime the
Textual panel uses. The only UI surface is a ``notify`` callback.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from novacode_cli.proactive import RUN_TIMEOUT, AgentSpec, execute_run, github_tags

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class ProactiveRuntime:
    """Two background loops over one `ProactiveStore`."""

    def __init__(
        self,
        store: Any,  # noqa: ANN401 — a ProactiveStore
        model_factory: Callable[[], Any],
        notify: Callable[..., None],
    ) -> None:
        """Start the run and tag loops immediately.

        Args:
            store: Open `ProactiveStore`.
            model_factory: Builds the model for one run.
            notify: Called with a message; accepts an optional ``title``.
        """
        self.store = store
        self.model_factory = model_factory
        self.notify = notify
        self.tasks = [asyncio.create_task(loop()) for loop in (self._runs, self._tags)]

    async def _runs(self) -> None:
        """Claim queued jobs forever, one at a time."""
        while True:
            try:
                job = await asyncio.to_thread(self.store.claim, time.time())
                if job is not None:
                    await self._run(job)
                    continue
            except sqlite3.Error:
                logger.exception("Proactive agent storage unavailable")
            await asyncio.sleep(2)

    async def _run(self, job: dict[str, Any]) -> None:
        """Execute one claimed job and persist its status and report."""
        name = AgentSpec.model_validate_json(job["spec"]).name
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
                model = await asyncio.to_thread(self.model_factory)
                result = await execute_run(job, model)
            status = "complete"
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish, job, "interrupted", "NovaCode closed.")
            raise
        except TimeoutError:
            status, result = "failed", "The agent reached its three-minute run limit."
        except Exception as exc:  # noqa: BLE001 — one agent must not stop later runs
            status = "failed"
            result = f"Run failed ({type(exc).__name__}). Check the model and tool setup."
        await asyncio.to_thread(self.store.finish, job, status, result)
        self.notify(
            f"{name} · {status}. Open /proactive to see the report.",
            title="Proactive agent",
        )

    async def _tags(self) -> None:
        """Poll watched GitHub repositories and queue runs for new tags."""
        while True:
            try:
                watch = await asyncio.to_thread(self.store.tag_watch, time.time())
                if watch:
                    key, definition, repo = watch
                    try:
                        tags = await github_tags(repo)
                    except (OSError, ValueError, TimeoutError):
                        logger.warning("Proactive tag check failed for %s", repo)
                        await asyncio.to_thread(self.store.tag_failure, key, definition)
                    else:
                        await asyncio.to_thread(self.store.record_tags, key, definition, tags)
                    continue
            except sqlite3.Error:
                logger.exception("Proactive tag storage unavailable")
            await asyncio.sleep(5)

    async def close(self) -> None:
        """Cancel both loops and wait for them to settle."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

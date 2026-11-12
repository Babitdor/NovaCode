"""Read-only SmithDB queries shared with the Harbor evaluation CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langsmith import Client


async def find_trial_run_ids(client: Client, project_name: str, trial_name: str) -> list[str]:
    """Find enough historical root runs to distinguish missing, unique and ambiguous.

    SmithDB otherwise defaults to the last day. Retain the legacy query's full
    history and stop after two matches, since feedback requires exactly one.
    """
    project = await client.aread_project(project_name=project_name)
    query = f'and(eq(metadata_key, "trial_name"), eq(metadata_value, {json.dumps(trial_name)}))'
    matches = []
    async for run in client.runs.query(
        project_ids=[str(project.id)],
        filter=query,
        is_root=True,
        min_start_time="1970-01-01T00:00:00Z",
        selects=["ID"],
        page_size=2,
    ):
        matches.append(str(run.id))
        if len(matches) == 2:  # noqa: PLR2004 - two results prove ambiguity
            break
    return matches

"""Owner-facing teammate task results and usage, outside model conversations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from rich.markdown import Markdown
from textual.widgets import Collapsible, Static

from novacode_cli.teammates.service import request

if TYPE_CHECKING:
    from novacode_cli.tracking.usage_tree import UsageNode
    from novacode_cli.tui.app import NovaApp

logger = logging.getLogger(__name__)


def task_markdown(task: dict) -> str:
    """Render only the public assignment and recorded result, never desktop inputs."""
    result = task.get("error") or task.get("full_output") or "No result recorded yet."
    heading = "Error" if task.get("error") else "Result"
    return (
        f"**Status:** {task['status'].capitalize()}\n\n"
        f"**Task**\n\n{task['text']}\n\n**{heading}**\n\n{result}"
    )


def usage_markdown(tasks: list[dict]) -> tuple[str, str]:
    """Sum distinct turn snapshots, including vision and resumed executions."""
    from novacode_cli.tracking.usage_tree import UsageTree

    totals = dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "calls"), 0
    )
    cost = 0.0
    unknown_cost = False
    missing_usage = 0
    rows = []

    def add_rows(node: UsageNode, scope: str = "") -> None:
        nonlocal missing_usage
        path = f"{scope} / {node.scope}" if scope else node.scope
        missing_usage += node.unavailable_calls
        if node.calls:
            label = path.replace("|", "\\|").replace("\n", " ")
            models = ", ".join(node.model_usage) or node.model or "Unknown"
            model = models.replace("|", "\\|").replace("\n", " ")
            rows.append(
                f"| {label} | {model} | {node.input_tokens:,} | {node.output_tokens:,} "
                f"| {node.calls:,} |"
            )
        for child in node.children:
            add_rows(child, path)

    for task in tasks:
        history = task.get("usage_history") or (
            {"turn": task["usage"]} if task.get("usage") else {}
        )
        for snapshot in history.values():
            tree = UsageTree.from_dict(snapshot)
            for key, value in tree.root.totals().items():
                totals[key] += value
            amount = tree.root.cost(recursive=True)
            unknown_cost |= amount is None
            cost += amount or 0
            add_rows(tree.root)
    if not totals["calls"]:
        return "No model usage recorded yet.", "Usage appears after a model call is recorded."
    price = "Unavailable" if unknown_cost else f"${cost:.4f}"
    call_label = "call" if totals["calls"] == 1 else "calls"
    summary = (
        f"**{totals['calls']:,} model {call_label} · "
        f"{totals['input_tokens'] + totals['output_tokens']:,} tokens**\n\n"
        f"Input: **{totals['input_tokens']:,}** · Output: **{totals['output_tokens']:,}**\n\n"
        f"Cache read: **{totals['cache_read_tokens']:,}** · "
        f"Cache creation: **{totals['cache_creation_tokens']:,}**\n\n"
        f"Estimated cost: **{price}**"
    )
    if missing_usage:
        summary += (
            f"\n\n{missing_usage:,} calls did not report token usage; token totals are partial."
        )
    detail = (
        "| Scope | Model | Input tokens | Output tokens | Calls |\n"
        "| :--- | :--- | ---: | ---: | ---: |\n"
        + "\n".join(rows)
        + "\n\nIncludes vision and resumed executions. Cached tokens are included in input "
        "totals, not added again. Cost is an estimate; unknown prices remain unavailable."
    )
    return summary, detail


class TeammateFeed:
    """Follow the profile worker while the TUI is connected, without owning its work."""

    def __init__(self, app: NovaApp) -> None:
        """Keep one collapsible per task and leave its expanded state to the user."""
        self.app = app
        self.cards: dict[tuple[str, str], Collapsible] = {}
        self.content: dict[tuple[str, str], tuple[str, str]] = {}
        self.hidden_history: set[tuple[str, str]] = set()
        self.connected: set[str] = set()

    async def refresh(self) -> None:
        """Observe without launching a worker or putting private history into the model."""
        try:
            result = await request("status", launch=False)
            roster = result.get("agents")
            if roster is None:
                roster = [result] if result.get("name") else []
            for state in roster:
                tasks = state.get("tasks", [])
                for index, task in enumerate(tasks):
                    key = (state["thread"], task["id"])
                    # Reconnect with unfinished work and the latest result only.
                    if (
                        state["thread"] not in self.connected
                        and index != len(tasks) - 1
                        and task["status"] in {"completed", "failed", "cancelled"}
                    ):
                        self.hidden_history.add(key)
                        continue
                    if key in self.hidden_history:
                        continue
                    await self._update(state, task, key)
                self.connected.add(state["thread"])
        except (OSError, RuntimeError, ValueError, aiohttp.ClientError):
            logger.debug("Teammate updates temporarily unavailable", exc_info=True)

    async def _update(self, state: dict, task: dict, key: tuple[str, str]) -> None:
        summary, _ = usage_markdown([task])
        pending = any(
            proposal.get("task") == task["id"] and proposal["state"] == "pending"
            for proposal in state.get("approvals", [])
        )
        status = "Input approval needed" if pending else task["status"].capitalize()
        title = f"{state['name']} · {status} · {' '.join(task['text'].split())[:70]}"
        body = task_markdown(task) + "\n\n### Usage\n\n" + summary
        if pending:
            body += (
                "\n\nOpen `/teammates` to review the saved observation and approve or deny input."
            )
        if self.content.get(key) == (title, body):
            return
        card = self.cards.get(key)
        if card is None or not card.is_mounted:
            card = Collapsible(
                Static(Markdown(body), classes="teammate-result"),
                title=title,
                collapsed=True,
                classes="tool teammate-update",
            )
            self.app._set_home_visible(visible=False)
            await self.app._mount(card)
            self.cards[key] = card
        else:
            card.title = title
            card.query_one(".teammate-result", Static).update(Markdown(body))
        self.content[key] = (title, body)

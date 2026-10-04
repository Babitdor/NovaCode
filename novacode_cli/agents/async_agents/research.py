"""Web research, in the background: the ``web-researcher`` specialist.

Searching and reading sources takes minutes and needs nothing from the session,
which makes it the clearest case for a background run.

Exports:
    graph: Compiled graph served by the async agent server.
"""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.agents.default_subagents.prompt import WEB_RESEARCHER

graph = build_specialist_graph(
    "research-agent",
    WEB_RESEARCHER,
    note=(
        "\nWrite the findings file as instructed, and ALSO give the key findings and their"
        " sources in your final message: here the reply is what gets reported back.\n"
    ),
)

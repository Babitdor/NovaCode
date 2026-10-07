"""Background specialist for evidence-based bug root-cause investigation."""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.prompts import render_template

DEFINITION = {
    "prompt": render_template("async_agents/bug_investigation.jinja"),
    "tools": [],
}

graph = build_specialist_graph(
    "bug-investigation-agent",
    DEFINITION,
    note=(
        "\nThis is a read-only investigation. Do not modify files; report evidence "
        "and likely causes.\n"
    ),
)

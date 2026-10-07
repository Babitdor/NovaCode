"""Background specialist for application performance and resource audits."""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.prompts import render_template

DEFINITION = {
    "prompt": render_template("async_agents/performance_audit.jinja"),
    "tools": [],
}

graph = build_specialist_graph(
    "performance-audit-agent",
    DEFINITION,
    note=(
        "\nThis is a read-only audit. Do not edit files or run benchmarks; give a "
        "measurement plan.\n"
    ),
)

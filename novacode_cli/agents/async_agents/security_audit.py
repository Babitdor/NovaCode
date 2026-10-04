"""Security audit, in the background.

A full audit reads most of a codebase, so it is the kind of job to start and
come back to. It only reads and searches.

Exports:
    graph: Compiled graph served by the async agent server.
"""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.prompts import render_template

DEFINITION = {
    "prompt": render_template("async_agents/security_audit.jinja"),
    "tools": ["duckduckgo_search", "docs_search", "fetch_url", "package_info"],
}

graph = build_specialist_graph(
    "security-audit-agent",
    DEFINITION,
    note="\nThis is an audit: report what you find, with file and line. Do not change any file.\n",
)

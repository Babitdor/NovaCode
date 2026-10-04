"""Security audit, in the background.

A full audit reads most of a codebase, so it is the kind of job to start and
come back to. It only reads and searches.

Exports:
    graph: Compiled graph served by the async agent server.
"""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.agents.default_subagents.prompt import SECURITY_AUDITOR_AGENT

graph = build_specialist_graph(
    "security-audit-agent",
    SECURITY_AUDITOR_AGENT,
    note="\nThis is an audit: report what you find, with file and line. Do not change any file.\n",
)

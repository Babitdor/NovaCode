"""Run the test suite in the background.

A suite can take minutes, and reading its failures needs nothing from the
session. The session's ``shell`` tool sits behind its approvals and sandbox,
neither of which reaches this server, so this agent gets ``run_tests`` instead:
test runners only, no shell.

Exports:
    graph: Compiled graph served by the async agent server.
"""

from __future__ import annotations

from novacode_cli.agents.async_agents._specialist import build_specialist_graph
from novacode_cli.agents.async_agents._test_commands import run_tests
from novacode_cli.agents.default_subagents.prompt import TESTING_AGENT

graph = build_specialist_graph(
    "test-runner-agent",
    TESTING_AGENT,
    extra_tools=[run_tests],
    note=(
        "\nThere is no `shell` tool in this run. Run tests with `run_tests`, which accepts"
        " test-runner commands only. Report failures and their likely cause; do not edit files.\n"
    ),
)

"""Long Code Mode turns use the shared graph limit; explicit overrides stay bounded."""

import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from novacode_cli.config.config import config


def test_default_graph_budget_allows_more_than_one_thousand_steps():
    class State(TypedDict):
        steps: int

    graph = StateGraph(State)
    graph.add_node("step", lambda state: {"steps": state["steps"] + 1})
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", lambda state: "step" if state["steps"] < 1001 else END)
    compiled = graph.compile().with_config(recursion_limit=config["recursion_limit"])
    assert compiled.invoke({"steps": 0})["steps"] == 1001


@pytest.mark.parametrize(
    ("override", "expected"),
    [(None, 9999), ("18000", 18000), ("0", 1), ("-5", 1), ("invalid", 9999)],
)
def test_environment_override_is_applied_at_startup(
    override: str | None,
    expected: int,
    tmp_path: Path,
):
    env = dict(os.environ)
    env["NOVA_HOME"] = str(tmp_path)
    env["PYTHONUTF8"] = "1"
    env.pop("NOVA_RECURSION_LIMIT", None)
    if override is not None:
        env["NOVA_RECURSION_LIMIT"] = override
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and source; only numeric config varies.
        [
            sys.executable,
            "-c",
            "from novacode_cli.config.config import config; print(config['recursion_limit'])",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert int(result.stdout.strip()) == expected

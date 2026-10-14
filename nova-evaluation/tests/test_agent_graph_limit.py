"""A productive agent must be able to use its Harbor time budget past 2,000 graph steps."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from deepagents_harbor.novacode_wrapper import AGENT_RECURSION_LIMIT


class _State(TypedDict):
    count: int


def test_graph_does_not_stop_at_the_old_limit() -> None:
    def advance(state: _State) -> _State:
        return {"count": state["count"] + 1}

    graph = StateGraph(_State)
    graph.add_node("advance", advance)
    graph.add_edge(START, "advance")
    graph.add_conditional_edges(
        "advance", lambda state: END if state["count"] == 2_100 else "advance"
    )

    result = graph.compile().invoke({"count": 0}, {"recursion_limit": AGENT_RECURSION_LIMIT})
    assert result["count"] == 2_100


if __name__ == "__main__":
    test_graph_does_not_stop_at_the_old_limit()

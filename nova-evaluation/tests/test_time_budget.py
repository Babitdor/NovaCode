"""A command may not use more than a share of the time left.

    cd nova-evaluation && .venv/Scripts/python tests/test_time_budget.py   (or pytest)
"""

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from deepagents_harbor.time_budget import TimeBudgetMiddleware


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def run(mw: TimeBudgetMiddleware, clock: Clock, args: dict, takes: float) -> tuple[dict, str]:
    seen: dict = {}

    def handler(request: ToolCallRequest) -> ToolMessage:
        seen.update(request.tool_call["args"])
        clock.now += takes
        return ToolMessage(content="out", tool_call_id="c1")

    call = {"name": "execute", "args": args, "id": "c1"}
    request = ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)
    return seen, mw.wrap_tool_call(request, handler).content


def test_a_command_gets_a_share_of_the_time_left() -> None:
    clock = Clock()
    mw = TimeBudgetMiddleware(12, clock=clock)  # 720s

    seen, out = run(mw, clock, {"command": "python greedy.py"}, takes=5)
    assert seen["timeout"] == 216  # 30% of 720s, not the 300s default
    assert "stopped" not in out

    seen, _ = run(mw, clock, {"command": "make", "timeout": 100}, takes=5)
    assert seen["timeout"] == 100  # a shorter request is left alone

    clock.now = 600  # two minutes left
    seen, out = run(mw, clock, {"command": "python greedy.py", "timeout": 300}, takes=60)
    assert seen["timeout"] == 60  # never under a minute
    assert "stopped at 60s" in out


def test_no_limit_means_no_cap() -> None:
    clock = Clock()
    seen, _ = run(TimeBudgetMiddleware(None, clock=clock), clock, {"command": "make"}, takes=1)
    assert "timeout" not in seen


if __name__ == "__main__":
    test_a_command_gets_a_share_of_the_time_left()
    test_no_limit_means_no_cap()
    print("ok")

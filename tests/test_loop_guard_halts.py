"""The loop guard's hard stop has to end the turn in a real agent graph.

It used to return ``Command(goto=END)`` from the tool node, which does not end
anything: the node's edge back to the model still fires. In an eval a stuck
model repeated one command 318 times, through 80 "halts", until the graph's
recursion limit killed the trial ungraded.
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from langchain.tools.tool_node import ToolCallRequest

from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

RUNS: list[str] = []


@tool
def search(pattern: str) -> str:
    """Search for a pattern."""
    RUNS.append(pattern)
    return "no matches"


class StuckModel(BaseChatModel):
    """Emits the identical tool call forever, whatever it is told."""

    calls: int = 0
    temperature: float = 0.0
    temperatures: list = []  # the temperature each call was bound with (None = unchanged)

    @property
    def _llm_type(self) -> str:
        return "stuck"

    def bind_tools(self, tools: Any, **kwargs: Any) -> StuckModel:  # noqa: ARG002
        self.temperatures.append(kwargs.get("temperature"))
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        self.calls += 1
        call = {"name": "search", "args": {"pattern": "needle"}, "id": f"c{self.calls}", "type": "tool_call"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


def test_a_stuck_model_is_stopped_not_left_to_hit_the_recursion_limit() -> None:
    RUNS.clear()
    model = StuckModel()
    agent = create_agent(model, tools=[search], middleware=[LoopGuardMiddleware(threshold=3)])

    out = agent.invoke({"messages": [("user", "find the needle")]}, {"recursion_limit": 200})

    assert len(RUNS) == 3  # the tool really ran three times, then was blocked
    assert model.calls == 7  # 3 real + 3 warnings + the one that was halted
    assert isinstance(out["messages"][-1], AIMessage) and "stopped" in out["messages"][-1].content.lower()

    # Before it comes to that: the second identical result says it is a repeat,
    # and from then on the model is sampled instead of asked the same question at 0.
    results = [m.content for m in out["messages"] if isinstance(m, ToolMessage)]
    assert "[note:" not in results[0] and "2 in a row" in results[1]
    assert model.temperatures[:2] == [None, None] and set(model.temperatures[2:]) == {0.7}


def _request(command: str) -> ToolCallRequest:
    call = {"name": "execute", "args": {"command": command}, "id": "c1"}
    return ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)


def test_the_same_script_under_a_new_name_each_time_is_still_a_loop() -> None:
    # Seen in evals: expCB.py, expCC.py, ... 274 times, each printing the same thing.
    script = "from bs4 import BeautifulSoup\nh = '<noscript><p title=x>x</p></noscript>'\nprint(repr(str(BeautifulSoup(h))))"
    output = "STR: '<noscript><p title=\"x\">x</p></noscript>'\n" * 6
    ran = []

    def handler(request: ToolCallRequest) -> ToolMessage:
        ran.append(request.tool_call["args"]["command"])
        return ToolMessage(content=output, tool_call_id="c1")

    mw = LoopGuardMiddleware(threshold=3)
    outs = [
        mw.wrap_tool_call(_request(f"cat > /tmp/exp{tag}.py <<'EOF'\n{script}\nEOF\npython /tmp/exp{tag}.py"), handler)
        for tag in ("CA", "CB", "CC", "CD", "CE", "CF", "CG")
    ]

    assert len(ran) == 3  # the fourth restatement is not run
    assert "3 in a row" in outs[2].content
    assert all("LOOP STOPPED" in o.content for o in outs[3:6])
    assert "LOOP HALTED" in outs[6].content  # the renames do not reset the escalation


def test_different_work_that_happens_to_print_the_same_thing_is_not_a_loop() -> None:
    same = "0 errors, 0 warnings\n" + "-" * 200
    mw = LoopGuardMiddleware(threshold=3)
    commands = ["ruff check src/", "mypy src/ --strict", "pyright src/", "pylint src/ --errors-only", "ruff check tests/"]
    outs = [
        mw.wrap_tool_call(_request(c), lambda r: ToolMessage(content=same, tool_call_id="c1")) for c in commands
    ]
    assert not any("LOOP STOPPED" in o.content for o in outs)


def test_a_short_result_repeating_is_ordinary() -> None:
    mw = LoopGuardMiddleware(threshold=3)
    outs = [
        mw.wrap_tool_call(_request(f"touch /tmp/marker{i} && echo done"), lambda r: ToolMessage(content="done", tool_call_id="c1"))
        for i in range(6)
    ]
    assert all(o.content == "done" for o in outs)


def test_a_long_script_renamed_each_time_is_a_loop_even_with_a_short_result() -> None:
    # probe159.py, probe160.py ... 150 times, each printing "CLICK ALERT: 1".
    script = "import subprocess, tempfile, time\nfrom selenium import webdriver\n" + "driver.get(url); time.sleep(1)\n" * 12
    mw = LoopGuardMiddleware(threshold=3)
    outs = [
        mw.wrap_tool_call(
            _request(f"cat > /tmp/probe{n}.py <<'EOF'\n{script}EOF\npython /tmp/probe{n}.py"),
            lambda r: ToolMessage(content="CLICK ALERT: 1", tool_call_id="c1"),
        )
        for n in range(157, 162)
    ]
    assert ["LOOP STOPPED" in o.content for o in outs] == [False, False, False, True, True]


def test_trying_a_different_payload_in_the_same_script_is_not_a_loop() -> None:
    # Honest trial and error: one line of a long script changes, the answer stays "NO ALERT".
    script = "import subprocess, tempfile, time\nfrom selenium import webdriver\n" + "driver.get(url); time.sleep(1)\n" * 12
    payloads = [
        "<svg><animate attributeName=href values=javascript:alert(1) /></svg>",
        "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>x</p></noscript>",
        "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)></style></mglyph></table></mtext></math>",
        "<form><button formaction=javascript:alert(1)>go</button></form>",
        "<iframe srcdoc='&lt;script&gt;alert(1)&lt;/script&gt;'></iframe>",
    ]
    mw = LoopGuardMiddleware(threshold=3)
    outs = [
        mw.wrap_tool_call(
            _request(f"cat > /tmp/try.py <<'EOF'\nPAYLOAD = {payload!r}\n{script}EOF\npython /tmp/try.py"),
            lambda r: ToolMessage(content="NO ALERT", tool_call_id="c1"),
        )
        for payload in payloads
    ]
    assert not any("LOOP STOPPED" in o.content for o in outs)


def test_a_stop_left_over_from_an_interrupted_turn_does_not_end_the_next_one():
    from langchain_core.messages import HumanMessage

    from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

    guard = LoopGuardMiddleware()
    guard._halt = True  # the hard stop fired, then the user interrupted

    assert guard.before_model({"messages": [HumanMessage("something new")]}, None) is None
    assert guard._halt is False


def test_comparing_two_large_calls_does_not_stall_the_ui_loop():
    """A character diff of two 40 KB heredocs took 23 s, on the loop the UI shares."""
    import json
    import time

    from novacode_cli.tracking.loop_guard import _restates

    script = "".join(f"my $v{n} = step({n});\n" for n in range(2_000))
    first = json.dumps({"command": f"cat > analyze1.pl <<'E'\n{script}E\nperl analyze1.pl"})
    renamed = first.replace("analyze1", "analyze2")
    rewritten = json.dumps({"command": "".join(f"print {n} * {n};\n" for n in range(3_300))})

    started = time.perf_counter()
    assert _restates(first, renamed, True)
    assert _restates(first, renamed, False)
    assert not _restates(first, rewritten, True)
    assert time.perf_counter() - started < 1.0

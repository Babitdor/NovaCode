"""``VerdictToolUsesEdit`` clears what a model judged stale, and nothing else.

The contract is one-sided on purpose: a missing verdict must keep the result.
That is what makes a cold cache, a slow model or an unreachable endpoint safe,
so the tests below pin the keep-side as hard as the clear-side.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.tool_offload import VerdictToolUsesEdit, cleared_dir
from novacode_cli.agents.tool_verdicts import ToolVerdictCache


def _turn(n: int, size: int = 100) -> list:
    return [
        HumanMessage(content=f"ask {n}", id=f"h{n}"),
        AIMessage(
            content="",
            id=f"a{n}",
            tool_calls=[
                {"id": f"c{n}", "name": "read_file", "args": {"file_path": f"/src/f{n}.py"}}
            ],
        ),
        ToolMessage(_payload(n) * size, tool_call_id=f"c{n}", name="read_file", id=f"t{n}"),
    ]


def _payload(n: int) -> str:
    """Content is a function of the call id, so a fixture can rebuild it."""
    return str(n)


def _history(turns: int = 4, size: int = 100) -> list:
    messages: list = []
    for n in range(1, turns + 1):
        messages.extend(_turn(n, size))
    return messages


def _big_tokens(_messages) -> int:  # noqa: ANN001 - any trigger is exceeded
    return 10**9


def _cache(tmp_path, verdicts: dict[str, float]) -> ToolVerdictCache:
    """A cache holding a verdict per given tool_call_id.

    The content is the turn's real payload, because the cache is keyed on it:
    a verdict stored against a placeholder is stored under a key no real message
    can produce, and every lookup then misses silently.
    """
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    for call_id, noul in verdicts.items():
        cache.put(
            ToolMessage(
                _payload(int(call_id[1:])) * 100,
                tool_call_id=call_id,
                name="read_file",
            ),
            noul,
        )
    return cache


def _contents(messages: list) -> list[str]:
    return [
        str(message.content) for message in messages if isinstance(message, ToolMessage)
    ]


def test_only_the_stale_results_are_cleared(tmp_path) -> None:
    messages = _history(4)
    # c1 stays needed, c2 and c3 are stale. c4 is inside keep=1's window.
    cache = _cache(tmp_path, {"c1": 0.9, "c2": 0.1, "c3": 0.1})
    edit = VerdictToolUsesEdit(
        trigger=0,
        keep=1,
        exclude_tools=["think"],
        placeholder="[cleared]",
        offload_dir=cleared_dir(tmp_path),
        verdicts=cache,
        keep_threshold=0.5,
    )

    edit.apply(messages, count_tokens=_big_tokens)

    contents = _contents(messages)
    assert contents[0] == "1" * 100, "a verdict above the threshold keeps it verbatim"
    assert "read_file-c2.txt" in contents[1], "stale results are offloaded"
    assert "read_file-c3.txt" in contents[2]
    assert contents[3] == "4" * 100, "the newest keep=1 result is never touched"
    assert "[cleared]" not in " ".join(contents), "the bare marker must never ship"


def test_an_unscored_result_is_kept_not_cleared(tmp_path) -> None:
    """The fail-open case: nothing judged it, so nothing may clear it."""
    messages = _history(3)
    cache = _cache(tmp_path, {"c1": 0.1})  # c2 and c3 have no verdict at all
    edit = VerdictToolUsesEdit(
        trigger=0,
        keep=0,
        placeholder="[cleared]",
        offload_dir=cleared_dir(tmp_path),
        verdicts=cache,
    )

    edit.apply(messages, count_tokens=_big_tokens)

    contents = _contents(messages)
    assert "read_file-c1.txt" in contents[0], "the judged one is cleared"
    assert contents[1] == "2" * 100, "unscored keeps its bytes"
    assert contents[2] == "3" * 100


def test_no_cache_at_all_changes_nothing(tmp_path) -> None:
    messages = _history(3)
    before = _contents(messages)
    edit = VerdictToolUsesEdit(
        trigger=0, keep=0, placeholder="[cleared]", verdicts=None
    )

    edit.apply(messages, count_tokens=_big_tokens)

    assert _contents(messages) == before


def test_below_the_trigger_no_verdict_is_consulted(tmp_path) -> None:
    """Clearing is the cheap reducer, but it still waits for context pressure."""
    messages = _history(3)
    cache = _cache(tmp_path, {"c1": 0.0, "c2": 0.0, "c3": 0.0})
    edit = VerdictToolUsesEdit(
        trigger=0,
        keep=0,
        placeholder="[cleared]",
        offload_dir=cleared_dir(tmp_path),
        verdicts=cache,
    )

    edit.apply(messages, count_tokens=lambda _m: 0)  # under trigger

    assert _contents(messages) == ["1" * 100, "2" * 100, "3" * 100]


def test_a_cache_that_errors_fails_open(tmp_path) -> None:
    """An unreadable cache must keep results, never clear on a guess."""

    class Exploding:
        def stale(self, _message, *, keep_threshold):  # noqa: ANN001
            raise OSError("cache is gone")

    messages = _history(3)
    edit = VerdictToolUsesEdit(
        trigger=0,
        keep=0,
        placeholder="[cleared]",
        offload_dir=cleared_dir(tmp_path),
        verdicts=Exploding(),
    )

    edit.apply(messages, count_tokens=_big_tokens)

    assert _contents(messages) == ["1" * 100, "2" * 100, "3" * 100]


def test_excluded_tools_are_never_cleared_even_when_judged_stale(tmp_path) -> None:
    """`think` is the model's own reasoning; the pool stays as configured."""
    reasoning = "reasoning " * 20
    messages = [
        HumanMessage(content="ask", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"id": "c1", "name": "think", "args": {}}],
        ),
        ToolMessage(content=reasoning, tool_call_id="c1", name="think", id="t1"),
    ]
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    cache.put(ToolMessage(content=reasoning, tool_call_id="c1", name="think"), 0.0)
    edit = VerdictToolUsesEdit(
        trigger=0,
        keep=0,
        exclude_tools=["think"],
        placeholder="[cleared]",
        offload_dir=cleared_dir(tmp_path),
        verdicts=cache,
    )

    edit.apply(messages, count_tokens=_big_tokens)

    assert _contents(messages) == [reasoning]

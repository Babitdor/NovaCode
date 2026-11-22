"""`/rewind`: pick a checkpoint, then rewrite the conversation and/or the files."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    import pytest

    from novacode_cli.ui.port import ScreenSpec


class _Agent:
    """Stand-in for the compiled graph: one checkpoint, one write per rewind."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.writes: list[dict[str, Any]] = []
        self.snapshot = SimpleNamespace(
            values={"messages": list(messages)},
            config={"configurable": {"checkpoint_id": "cp-1"}},
        )

    async def aget_state(self, _config: dict[str, Any]) -> SimpleNamespace:
        return self.snapshot

    async def aupdate_state(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def _history() -> list[Any]:
    return [
        HumanMessage(content="add a parser", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"id": "call-1", "name": "edit_file", "args": {"path": "x.py"}}],
        ),
        ToolMessage(content="edited x.py", tool_call_id="call-1", id="t1"),
        AIMessage(content="Done: x.py parses.", id="a2"),
        HumanMessage(content="now add tests", id="h2"),
        AIMessage(content="Added two tests.", id="a3"),
    ]


def _worker(agent: _Agent | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent if agent is not None else _Agent(_history()),
        session_state=SimpleNamespace(thread_id="t1"),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


def _port(
    *, answers: list[object] | None = None, busy: bool = False, agent: _Agent | None = None
) -> tuple[ServerPort, list[ScreenSpec]]:
    queue = list(answers or [])
    seen: list[ScreenSpec] = []

    async def ask_screen(spec: ScreenSpec) -> object:
        seen.append(spec)
        return queue.pop(0) if queue else None

    port = ServerPort(  # type: ignore[arg-type]  # a worker stand-in is the contract here
        lambda _frame: None,
        _worker(agent),
        ask_screen=ask_screen,
        busy=lambda: busy,
    )
    return port, seen


def _changes(_thread: str, _calls: set[str]) -> dict[str, str | None]:
    return {"x.py": "blob-1"}


async def test_rewind_refuses_while_a_turn_runs() -> None:
    port, seen = _port(busy=True)
    result = await port.run("/rewind", None)
    assert result.ok is False
    assert result.message == "Stop the running turn first (Esc), then /rewind."
    assert seen == []


async def test_rewind_with_no_user_turns_says_so() -> None:
    agent = _Agent([AIMessage(content="hello", id="a1")])
    port, seen = _port(agent=agent)
    result = await port.run("/rewind", None)
    assert result.ok is False
    assert result.message == "Nothing to rewind to yet."
    assert seen == []


async def test_rewind_lists_user_turns_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", _changes)
    port, seen = _port(answers=[None])
    await port.run("/rewind", None)
    items = seen[0].payload["items"]
    assert [item["id"] for item in items] == ["4", "0"]
    assert items[0]["label"] == "now add tests"
    assert items[0]["detail"] == "1 file(s) changed since"


async def test_rewind_conversation_only_keeps_the_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored: list[object] = []
    monkeypatch.setattr("novacode_cli.rewind.changes_after", _changes)
    monkeypatch.setattr(
        "novacode_cli.rewind.restore", lambda _t, before: restored.extend(before) or []
    )
    agent = _Agent(_history())
    port, _seen = _port(answers=[{"id": "4"}, {"id": "conversation"}], agent=agent)
    result = await port.run("/rewind", None)
    assert result.ok is True
    assert restored == []
    assert result.message == "⟲ Rewound to before “now add tests”: conversation rewound."
    (write,) = agent.writes
    values = write["values"]
    assert write["as_node"] == "model"
    assert write["config"] == {"configurable": {"thread_id": "t1"}}
    assert [m.id for m in values["messages"][1:]] == ["h1", "a1", "t1", "a2"]
    assert result.action == "clear"
    labels = [block.text for block in result.blocks if block.kind == "heading"]
    assert labels == ["you", "nova", "your message, ready to send again"]


async def test_rewind_both_restores_the_recorded_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str | None]] = []

    def restore(_thread: str, before: dict[str, str | None]) -> list[str]:
        calls.append(before)
        return list(before)

    monkeypatch.setattr("novacode_cli.rewind.changes_after", _changes)
    monkeypatch.setattr("novacode_cli.rewind.restore", restore)
    agent = _Agent(_history())
    port, _seen = _port(answers=[{"id": "4"}, {"id": "both"}], agent=agent)
    result = await port.run("/rewind", None)
    assert calls == [{"x.py": "blob-1"}]
    assert len(agent.writes) == 1
    assert result.message == (
        "⟲ Rewound to before “now add tests”: conversation rewound, 1 file(s) restored."
    )


async def test_rewind_code_only_leaves_the_conversation_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", _changes)
    monkeypatch.setattr("novacode_cli.rewind.restore", lambda _t, before: list(before))
    agent = _Agent(_history())
    port, _seen = _port(answers=[{"id": "4"}, {"id": "code"}], agent=agent)
    result = await port.run("/rewind", None)
    assert agent.writes == []
    assert result.action is None
    assert result.message == "⟲ Rewound to before “now add tests”: 1 file(s) restored."


async def test_rewind_with_unchanged_files_does_not_ask_what_to_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", lambda _t, _c: {})
    agent = _Agent(_history())
    port, seen = _port(answers=[{"id": "0"}], agent=agent)
    result = await port.run("/rewind", None)
    assert len(seen) == 1
    assert result.ok is True
    assert len(agent.writes) == 1


async def test_rewind_cancelled_at_the_picker_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", _changes)
    agent = _Agent(_history())
    port, _seen = _port(answers=[None], agent=agent)
    result = await port.run("/rewind", None)
    assert agent.writes == []
    assert result.ok is True
    assert result.message == "Cancelled."


async def test_rewind_drops_a_stale_summary_when_the_cut_breaks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", lambda _t, _c: {})
    messages = _history()
    agent = _Agent(messages)
    agent.snapshot.values["_summarization_event"] = {"cutoff_index": 3}
    # Rewinding to the first message keeps nothing the summary could describe.
    port, _seen = _port(answers=[{"id": "0"}], agent=agent)
    await port.run("/rewind", None)
    (write,) = agent.writes
    assert write["values"]["_summarization_event"] is None


async def test_rewind_keeps_the_summary_when_the_cut_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novacode_cli.rewind.changes_after", lambda _t, _c: {})
    agent = _Agent(_history())
    agent.snapshot.values["_summarization_event"] = {"cutoff_index": 2}
    port, _seen = _port(answers=[{"id": "4"}], agent=agent)
    await port.run("/rewind", None)
    (write,) = agent.writes
    assert "_summarization_event" not in write["values"]
